"""任务 7 的规则版本与纯函数判定（spec R09–R13、R15–R17、R20–R21、R24–R33、R34–R35、R39、R42、R45，规则版本 t7r1）。不访问数据库。

RuleSet 集中所有默认值：置信度、信号阈值、片长容差、发行商后缀与区域优先级。改任何数值就是新规则版本：
用 RuleSet.with_version 派生并登记到 RULESETS。四组字段分别构成四个阶段键的“规则签名”（design“输入键与阶段键”），
所以只改片长容差时只有 movie_scalar 的阶段键变化，改版本层规则则整条链重算（R51）。
改动决策算法本身（不只是数值）须同时升 ALGORITHM 里对应组的标记，使旧阶段结果不会被误当作新规则的结果复用。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from etl_task4.normalize import ARTICLES, classify_segment, fold, title_key

from . import CURRENT_RULE_VERSION, RULE_VERSION

# 各阶段决策算法的标记：算法改动时升这里（与数值无关），进入对应阶段的规则签名
ALGORITHM = {"edition": "e1", "link": "l1", "scalar": "s1", "multivalue": "m2"}

CONFLICT_TYPES = (
    "none", "formatting_or_alias", "unrated_vs_rated", "minor_tolerance", "framerate_or_edition",
    "cross_source_override", "complementary_union", "director_source_disagreement", "billing_conflict_resolved",
    "semantic_distinction", "intra_asin_publisher_conflict", "intra_edition_rating_conflict",
    "intra_asin_runtime_conflict", "cross_source_mismatch_halt", "severe_mismatch_unresolved",
    "intra_source_tie_unresolved",
)    # R13 闭集，与 sql/601 的 CHECK 一致

CANDIDATE_STATUSES = ("adopted", "rejected", "conflict_pending", "superseded_by_specific")      # R08
SELECTION_STATUSES = ("resolved", "conflict_unresolved", "missing")                              # R11
SOURCE_RELIABILITY_KEYS = {"imdb_t4_link": "conf_t4_link", "imdb_t5_hint": "conf_t5_hint",
                           "imdb_t6_cooccur": "conf_t6_cooccur"}
WEAK_SOURCES = ("imdb_t5_hint", "imdb_t6_cooccur")                                                # R14：弱匹配

EDITION_FIELDS = ("section_priority", "publisher_suffixes", "conf_agree", "conf_single", "conf_alias",
                  "conf_priority", "conf_semantic", "conf_flagged_date")
LINK_FIELDS = ("conf_t4_link", "conf_t5_hint", "conf_t6_cooccur", "year_gap", "runtime_gap", "multidisc_ratio",
               "subset_min_words")
SCALAR_FIELDS = ("runtime_tol_small", "runtime_tol_variance", "runtime_tol_unexplained", "range_mode_max",
                 "pal_numerator", "pal_denominator", "pal_slack", "multidisc_ratio", "conf_override",
                 "conf_tolerance", "conf_page_only", "penalty_single_signal")
MULTIVALUE_FIELDS = ("align_levels", "conf_union", "conf_imdb_only")


@dataclass(frozen=True)
class RuleSet:
    version: str
    # ---- 版本层（edition_resolve，R44–R48）
    section_priority: tuple[str, ...] = ("detail_bullets", "product_overview", "product_details", "byline")  # R44：区域优先级
    publisher_suffixes: tuple[str, ...] = ("home video", "home entertainment", "pictures", "studios",
                                           "entertainment", "inc", "corp")                                   # R45
    conf_agree: float = 0.95            # 全部候选一致
    conf_single: float = 0.90           # 单一来源
    conf_alias: float = 0.85            # 别名或写法不同但同一值（formatting_or_alias）
    conf_priority: float = 0.70         # 区域优先级决出
    conf_semantic: float = 0.80         # 发售日与上架日并存（semantic_distinction）
    conf_flagged_date: float = 0.60     # R46：带 after_review_period / jan_first_suspect 标记
    # ---- 关联评估（link_assess，R14–R17）
    conf_t4_link: float = 0.95          # R14：三类 IMDb 关联的基础可靠度
    conf_t5_hint: float = 0.80
    conf_t6_cooccur: float = 0.75
    year_gap: int = 2                   # R15：Y 信号的年份差下限
    runtime_gap: int = 15               # R15：RT 信号的分钟差上限（超过才亮）
    multidisc_ratio: float = 1.5        # 多碟花絮版：页面片长 ≥ 该倍数 × IMDb（R15、R29）
    subset_min_words: int = 2           # T 信号：实词子集至少需要的实词数
    # ---- 作品层标量（movie_scalar，R20–R33）
    runtime_tol_small: int = 2          # R29：tolerance_1_2 的上限
    runtime_tol_variance: int = 5       # R29：variance_3_5 的上限
    runtime_tol_unexplained: int = 15   # R29：unexplained_6_15 的上限
    range_mode_max: int = 5             # R32：无 IMDb 时多版本极差在此之内取众数
    pal_numerator: int = 24             # R30：PAL 加速换算 24/25
    pal_denominator: int = 25
    pal_slack: int = 1
    conf_override: float = 0.70         # R25：cross_source_override 的置信度上限
    conf_tolerance: float = 0.85        # R24、R32：minor_tolerance
    conf_page_only: float = 0.80        # 无 IMDb 时只来自页面
    penalty_single_signal: float = 0.10  # R18：弱匹配单信号仍采用 IMDb 时的置信度扣减
    # ---- 作品层多值（movie_multivalue，R34–R43；T7-04 起使用）
    align_levels: tuple[str, ...] = ("L1", "L2", "L3", "LI", "L4")   # R38：等级序，越靠前越严格
    conf_union: float = 0.90
    conf_imdb_only: float = 0.80

    def with_version(self, version: str, **over: Any) -> "RuleSet":
        return dataclasses.replace(self, version=version, **over)

    def thresholds(self) -> dict[str, Any]:
        return {k: v for k, v in dataclasses.asdict(self).items() if k != "version"}

    def reliability(self, source: str) -> float:
        """R14：IMDb 关联来源的基础可靠度。"""
        return float(getattr(self, SOURCE_RELIABILITY_KEYS[source]))

    def signature(self, group: str) -> str:
        """某阶段规则签名：该组字段的取值与算法标记的摘要。组：edition、link、scalar、multivalue。"""
        fields = {"edition": EDITION_FIELDS, "link": LINK_FIELDS, "scalar": SCALAR_FIELDS,
                  "multivalue": MULTIVALUE_FIELDS}[group]
        payload = {name: getattr(self, name) for name in fields}
        payload["algorithm"] = ALGORITHM[group]
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=list)
        return f"{group[0]}{hashlib.sha256(raw.encode()).hexdigest()[:12]}"


DEFAULT_RULES = RuleSet(RULE_VERSION)
RULESETS: dict[str, RuleSet] = {DEFAULT_RULES.version: DEFAULT_RULES}
assert CURRENT_RULE_VERSION in RULESETS


def ruleset(version: str) -> RuleSet:
    try:
        return RULESETS[version]
    except KeyError:
        raise ValueError(f"未登记的规则版本 {version!r}，已登记：{sorted(RULESETS)}") from None


# ---------------------------------------------------------------- 标题（R20、R21、R15 的 T 信号）

# 括号段里这些说明不是标题的一部分，除了任务 4 词表已覆盖的类别（载体、年份、语言、地区、包装、剪辑版），再加 Bilingual
_EXTRA_REMOVABLE = frozenset({"bilingual"})
_BRACKET = re.compile(r"\[[^\[\]]*\]|\((?:[^()]|\([^()]*\))*\)")
_SPACES = re.compile(r"\s+")
_TRAIL = re.compile(r"[\s\-–—:,]+$")


def clean_page_display_title(raw: str) -> str:
    """页面标题 → 清洁标题（R20）：去掉 [VHS]、[DVD]、(Widescreen Edition)、(Full Screen)、(Collector's Edition)、
    (Bilingual) 与括号里的年份等说明，保留卷号、副标题和品牌括号（如 (Criterion Collection)）。原值永远不改。"""
    def drop(m: re.Match) -> str:
        inner = m.group(0)[1:-1].strip()
        if inner.casefold() in _EXTRA_REMOVABLE:
            return " "
        category, _attrs, year, covered = classify_segment(inner)
        if category == "brand" or not covered and year is None:
            return m.group(0)
        return " "

    out = _BRACKET.sub(drop, raw)
    out = _TRAIL.sub("", _SPACES.sub(" ", out).strip())
    return out or raw.strip()


CARRIER_TOKENS = frozenset({"vhs", "dvd", "dvds", "umd", "laserdisc"})
STOPWORDS = frozenset(ARTICLES) | {"and", "of", "in", "on", "at", "to", "for", "with", "from", "by", "or", "is", "as"}


def signal_tokens(key: str) -> list[str]:
    """标题键 → T 信号用的词序列：去载体词（vhs、dvd、blu-ray、hd dvd）与首尾冠词（R15）；只剩空时保留原词。"""
    toks = key.split()
    out: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if t == "blu" and nxt == "ray":
            i += 2
            continue
        if t == "hd" and nxt == "dvd":
            i += 2
            continue
        if t in CARRIER_TOKENS or t == "bluray":
            i += 1
            continue
        out.append(t)
        i += 1
    if not out:
        out = toks[:]
    while len(out) > 1 and out[0] in ARTICLES:
        out.pop(0)
    while len(out) > 1 and out[-1] in ARTICLES:
        out.pop()
    return out


def content_words(tokens: Sequence[str]) -> set[str]:
    return {t for t in tokens if t not in STOPWORDS}


def title_relation(a: Sequence[str], b: Sequence[str], min_words: int = 2) -> str | None:
    """两个标题词序列的关系：equal（相等）、sub（实词集合双向子集，且较小一方至少 min_words 个实词）、None。
    单实词标题只能相等：Wallace 不算 Wallace & Gromit 的子集（任务 6 同类假阳性的教训）。"""
    if list(a) == list(b):
        return "equal"
    ca, cb = content_words(a), content_words(b)
    if ca and cb and (ca <= cb and len(ca) >= min_words or cb <= ca and len(cb) >= min_words):
        return "sub"
    return None


def title_signal(page_keys: Iterable[str], imdb_keys: Iterable[str], min_words: int = 2) -> str:
    """T 信号（R15）：页面标题键与 IMDb 全部标题键去载体词与首尾冠词后，任一对相等为 ok，任一对双向实词子集为
    sub，都不是为 bad；任一侧没有标题为 na。"""
    page = [signal_tokens(k) for k in page_keys if k]
    imdb = [signal_tokens(k) for k in imdb_keys if k]
    if not page or not imdb:
        return "na"
    relations = {title_relation(p, i, min_words) for p in page for i in imdb}
    if "equal" in relations:
        return "ok"
    if "sub" in relations:
        return "sub"
    return "bad"


def title_match_class(page_title: str, primary: str | None, original: str | None, akas: Iterable[str],
                      min_words: int = 2, page_key: str | None = None) -> str:
    """R20：页面清洁标题与 IMDb 标题的关系。same = 清洁标题的规范键与 primary_title 相同；alias = 命中 original、aka、
    与任一标题实词子集，或任务 4 规范键（page_key，断言的 normalized_value）命中 IMDb 任一标题键；other = 无关。
    page_key 只能把 other 升为 alias，不放宽 same 的条件。"""
    key = title_key(page_title)
    if primary and key == title_key(primary):
        return "same"
    others = [t for t in (original, *akas) if t]
    if any(key == title_key(t) for t in others):
        return "alias"
    if page_key and any(page_key == title_key(t) for t in (primary, *others) if t):
        return "alias"
    page_tokens = signal_tokens(key)
    if any(title_relation(page_tokens, signal_tokens(title_key(t)), min_words) is not None
           for t in (primary, *others) if t):
        return "alias"
    return "other"


_VOLUME = re.compile(r"\b(?:vol(?:ume)?\.?|part|disc|book|episode)\s*\d+\b", re.I)


def _split_subtitle(clean: str) -> tuple[str, str]:
    m = re.search(r"\s+[-–—]\s+|\s*:\s+", clean)
    if not m:
        return title_key(clean), ""
    return title_key(clean[:m.start()]), title_key(clean[m.end():])


def titles_compatible(a: str, b: str) -> bool:
    """R21：两个清洁标题的规范键一致，或互为副标题、卷号包含时视为同一标题的不同写法；
    续集编号不同（Rocky 与 Rocky II）不算。"""
    ka, kb = title_key(a), title_key(b)
    if ka == kb:
        return True
    ma, sa = _split_subtitle(a)
    mb, sb = _split_subtitle(b)
    if ma == mb and (not sa or not sb):
        return True
    ra, rb = title_key(_VOLUME.sub(" ", a)), title_key(_VOLUME.sub(" ", b))
    return ra == rb and (_VOLUME.search(a) is not None or _VOLUME.search(b) is not None)


@dataclass(frozen=True)
class PageTitle:
    asin: str
    clean: str


def choose_page_title(titles: Sequence[PageTitle]) -> tuple[str | None, str]:
    """R21：无 IMDb 或已熔断时只用页面候选。返回 (胜出的清洁标题, 说明)；实质不同返回 (None, 'distinct')。
    规范键一致或互为副标题、卷号包含时取频次最高的清洁写法，平票取最小 ASIN；这里只在同一标题的写法间选展示形式。"""
    if not titles:
        return None, "none"
    # 两两兼容经传递相连才算同一标题（并查集式合并）
    groups: list[list[PageTitle]] = []
    for t in titles:
        placed = [g for g in groups if any(titles_compatible(t.clean, o.clean) for o in g)]
        merged = [t] + [o for g in placed for o in g]
        groups = [g for g in groups if g not in placed] + [merged]
    if len(groups) > 1:
        return None, "distinct"
    freq = Counter(t.clean for t in titles)
    first_asin: dict[str, str] = {}
    for t in titles:
        first_asin[t.clean] = min(t.asin, first_asin.get(t.clean, t.asin))
    best = sorted(freq, key=lambda c: (-freq[c], first_asin[c]))[0]
    return best, "alias" if len({title_key(t.clean) for t in titles}) > 1 else "same"


# ---------------------------------------------------------------- 内容分级（R33）

CONCRETE_RATINGS = ("G", "PG", "PG-13", "R", "NC-17", "X")
UNRATED = "NR/Unrated"
RATINGS = (*CONCRETE_RATINGS, UNRATED)
_UNRATED_WORDS = frozenset({"NR", "UNRATED", "NOT RATED", "UNRATED (NOT RATED)", "NR (NOT RATED)"})
_RATING_FORMS = {"PG13": "PG-13", "PG 13": "PG-13", "NC17": "NC-17", "NC 17": "NC-17"}


def normalize_mpaa(raw: str | None) -> str | None:
    """页面分级串 → 受控分级（R33）：简称与全称（PG、PG (Parental Guidance Suggested)）同值；NR、Not Rated、Unrated
    写法都规范为 NR/Unrated；不认识的返回 None（调用方写 unknown_mpaa_rating，不猜测）。"""
    if raw is None:
        return None
    s = _SPACES.sub(" ", raw.strip()).upper()
    if not s:
        return None
    if s in _UNRATED_WORDS or s.startswith(("NR ", "UNRATED ")):
        return UNRATED
    head = re.sub(r"\s*\(.*\)\s*$", "", s).strip()
    head = _RATING_FORMS.get(head, head)
    if head in CONCRETE_RATINGS:
        return head
    if head in ("NR", "UNRATED", "NOT RATED"):
        return UNRATED
    return None


def is_concrete_rating(rating: str | None) -> bool:
    return rating in CONCRETE_RATINGS


@dataclass(frozen=True)
class RatingDecision:
    status: str                       # resolved | conflict_unresolved
    value: str | None
    conflict_type: str
    rule: str
    reason: str


def decide_movie_rating(ratings: Iterable[str]) -> RatingDecision:
    """R33：作品层只聚合 edition 商品。一致则采用；单一具体分级与 NR/Unrated 并存采用具体分级；两种以上具体分级保留
    冲突（IMDb 基础表没有分级，没有第三方可仲裁）。输入为各商品已规范化的分级（至少一个）。"""
    distinct = sorted(set(ratings))
    if len(distinct) == 1:
        return RatingDecision("resolved", distinct[0], "none", "MR1", "所有 edition 商品分级一致")
    concrete = [r for r in distinct if is_concrete_rating(r)]
    if len(concrete) == 1:
        return RatingDecision("resolved", concrete[0], "unrated_vs_rated", "MR2",
                              f"单一具体分级 {concrete[0]} 与未评级写法并存，采用具体分级")
    return RatingDecision("conflict_unresolved", None, "intra_source_tie_unresolved", "MR3",
                          f"存在 {len(concrete)} 种具体分级 {concrete}，没有可靠依据裁决，保留冲突")


# ---------------------------------------------------------------- 片长（R29–R32）

DIFF_CLASSES = ("exact", "tolerance_1_2", "pal_evidence", "variance_3_5", "cut_or_multidisc", "unexplained_6_15",
                "unexplained_gt15")
DIFF_CONFLICT_TYPE = {"exact": "none", "tolerance_1_2": "minor_tolerance", "pal_evidence": "framerate_or_edition",
                      "variance_3_5": "framerate_or_edition", "cut_or_multidisc": "framerate_or_edition",
                      "unexplained_6_15": "cross_source_override", "unexplained_gt15": "cross_source_override"}


def pal_runtime(imdb_minutes: int, rules: RuleSet = DEFAULT_RULES) -> int:
    """R30：IMDb 片长按 PAL 加速（24→25 帧）换算，四舍五入到整数分钟。"""
    return (2 * imdb_minutes * rules.pal_numerator + rules.pal_denominator) // (2 * rules.pal_denominator)


def has_pal(video_systems: Iterable[str]) -> bool:
    return any("pal" in re.split(r"[^a-z0-9]+", (v or "").casefold()) for v in video_systems)


def is_multidisc_extra(page: int, imdb: int, discs: int | None, rules: RuleSet = DEFAULT_RULES) -> bool:
    return bool(discs and discs >= 2 and page >= rules.multidisc_ratio * imdb)


def classify_runtime_diff(page: int, imdb: int, *, cut_types: Sequence[str] = (), discs: int | None = None,
                          has_pal_evidence: bool = False, rules: RuleSet = DEFAULT_RULES) -> tuple[str, bool]:
    """R29–R30：页面片长与 IMDb 的差异类别与 pal_formula_match。分类顺序 exact → tolerance_1_2 → pal_evidence →
    variance_3_5 → cut_or_multidisc → unexplained_6_15 → unexplained_gt15。pal_evidence 必须同时有 PAL 断言与
    公式吻合；只有公式吻合不归为 PAL，仍按差值落入后面的类别，并记 pal_formula_match = true。"""
    ad = abs(page - imdb)
    pal_formula = abs(page - pal_runtime(imdb, rules)) <= rules.pal_slack
    if ad == 0:
        cls = "exact"
    elif ad <= rules.runtime_tol_small:
        cls = "tolerance_1_2"
    elif has_pal_evidence and pal_formula:
        cls = "pal_evidence"
    elif ad <= rules.runtime_tol_variance:
        cls = "variance_3_5"
    elif cut_types or is_multidisc_extra(page, imdb, discs, rules):
        cls = "cut_or_multidisc"
    elif ad <= rules.runtime_tol_unexplained:
        cls = "unexplained_6_15"
    else:
        cls = "unexplained_gt15"
    return cls, pal_formula


def runtime_signal_lit(page: int, imdb: int, *, cut_types: Sequence[str] = (), discs: int | None = None,
                       rules: RuleSet = DEFAULT_RULES) -> bool:
    """R15：RT 信号对一个 edition 商品亮起：与 IMDb 差超过 runtime_gap 分钟，且没有剪辑版标记、也不是多碟花絮版。"""
    return (abs(page - imdb) > rules.runtime_gap and not cut_types
            and not is_multidisc_extra(page, imdb, discs, rules))


def worst_diff_class(classes: Iterable[str]) -> str | None:
    """R31：作品层取差异最严重的版本。"""
    ranked = [DIFF_CLASSES.index(c) for c in classes]
    return DIFF_CLASSES[max(ranked)] if ranked else None


@dataclass(frozen=True)
class PageRuntime:
    asin: str
    minutes: int
    has_cut: bool = False


@dataclass(frozen=True)
class RuntimeDecision:
    status: str
    value: int | None
    conflict_type: str
    rule: str
    reason: str


def decide_runtime_without_imdb(pages: Sequence[PageRuntime], rules: RuleSet = DEFAULT_RULES) -> RuntimeDecision:
    """R32：无 IMDb 片长时只用 edition 商品。单版本或一致直接采用；多版本极差 ≤ range_mode_max 取众数，平票取较小值
    （差值在舍入误差内，不是跨值投票）；极差更大时采用无剪辑标记版本的众数，无法区分则保留冲突。"""
    values = [p.minutes for p in pages]
    if not values:
        return RuntimeDecision("missing", None, "none", "MN0", "没有 edition 商品片长")
    if len(set(values)) == 1:
        return RuntimeDecision("resolved", values[0], "none", "MN1", "单一版本或各版本片长一致")
    if max(values) - min(values) <= rules.range_mode_max:
        best = _mode(values)
        return RuntimeDecision("resolved", best, "minor_tolerance", "MN2",
                               f"各版本极差 {max(values) - min(values)} 分钟在舍入误差内，取众数，平票取较小值")
    plain = [p.minutes for p in pages if not p.has_cut]
    if plain and len(plain) < len(values):
        counts = Counter(plain)
        top = max(counts.values())
        modes = sorted(v for v, c in counts.items() if c == top)
        if len(modes) == 1:
            return RuntimeDecision("resolved", modes[0], "framerate_or_edition", "MN3",
                                   "极差较大，采用无剪辑标记版本的众数")
    return RuntimeDecision("conflict_unresolved", None, "intra_source_tie_unresolved", "MN4",
                           f"各版本片长 {sorted(set(values))} 极差 {max(values) - min(values)} 分钟，"
                           "且无法用剪辑版标记区分，保留冲突")


def _mode(values: Sequence[int]) -> int:
    counts = Counter(values)
    top = max(counts.values())
    return min(v for v, c in counts.items() if c == top)


# ---------------------------------------------------------------- 上映年份决策表（R24–R27）

@dataclass(frozen=True)
class YearInputs:
    imdb_year: int | None             # 有效 tconst 的 IMDb 年份；没有关联为 None
    weak: bool = False
    halted: bool = False
    page_years: tuple[int, ...] = ()  # 页面标题年份候选
    sig_year: str = "na"
    sig_director: str = "na"
    sig_runtime: str = "na"
    close_runtime: bool = False       # 存在页面与 IMDb 片长差 ≤ runtime_gap 的 edition 商品


@dataclass(frozen=True)
class YearDecision:
    status: str                       # resolved | conflict_unresolved | missing
    year: int | None
    source: str | None                # 采用的来源：imdb | page
    conflict_type: str
    rule: str
    reason: str
    issue: str | None = None          # 需要写的质量问题代码


def decide_release_year(i: YearInputs) -> YearDecision:
    """设计第 4 节的决策表。推断上界不是候选，不进入这里（R27）。"""
    pages = sorted(set(i.page_years))
    if i.imdb_year is None:
        if not pages:
            return YearDecision("missing", None, None, "none", "YR10", "没有任何年份候选")
        if len(pages) == 1:
            return YearDecision("resolved", pages[0], "page", "none", "YR8", "无 IMDb 关联，页面标题年份一致")
        return YearDecision("conflict_unresolved", None, None, "intra_source_tie_unresolved", "YR9",
                            f"无 IMDb 关联，页面标题年份 {pages} 不一致，没有可靠依据裁决")
    if i.halted:
        return YearDecision("conflict_unresolved", None, None, "cross_source_mismatch_halt", "YR7",
                            "弱匹配关联被判定为错配（熔断），IMDb 年份不可信，页面年份也不足以裁决")
    gaps = [abs(p - i.imdb_year) for p in pages]
    if i.sig_year != "bad":
        if not gaps or max(gaps) == 0:
            return YearDecision("resolved", i.imdb_year, "imdb", "none", "YR1", "IMDb 与页面年份一致或只有 IMDb")
        if max(gaps) <= 1:
            return YearDecision("resolved", i.imdb_year, "imdb", "minor_tolerance", "YR2",
                                "IMDb 与页面标题年份相差 1 年，采用 IMDb")
    if not i.weak:
        if i.sig_director == "bad" and i.sig_runtime == "bad":
            return YearDecision("conflict_unresolved", None, None, "severe_mismatch_unresolved", "YR3",
                                "强关联但年份、导演、片长同时矛盾，不能确认是同一部片", "severe_mismatch_unresolved")
        return YearDecision("resolved", i.imdb_year, "imdb", "cross_source_override", "YR4",
                            "强关联：年份差异在 2 年以上，导演或片长没有同时矛盾，采用 IMDb", "cross_source_override")
    if i.sig_director == "ok" or i.close_runtime:
        return YearDecision("resolved", i.imdb_year, "imdb", "cross_source_override", "YR5",
                            "弱匹配未熔断：导演兼容或存在差 ≤15 分钟的片长，作为正面佐证采用 IMDb", "cross_source_override")
    return YearDecision("conflict_unresolved", None, None, "severe_mismatch_unresolved", "YR6",
                        "弱匹配未熔断，年份差异在 2 年以上且没有导演或片长佐证，保留冲突", "severe_mismatch_unresolved")


# ---------------------------------------------------------------- 关联评估（R15–R18）

SIGNAL_NAMES = {"year": "Y", "runtime": "RT", "director": "D", "title": "T"}


def decide_halt(weak: bool, y: bool, rt: bool, d: bool, t: bool) -> tuple[bool, str | None]:
    """R17：弱匹配且 (Y ∧ (RT ∨ D ∨ T)) ∨ (T ∧ (RT ∨ D)) 时熔断；强关联永不熔断。返回 (熔断, 原因)。"""
    if not weak:
        return False, None
    if (y and (rt or d or t)) or (t and (rt or d)):
        lit = [n for n, on in (("Y", y), ("RT", rt), ("D", d), ("T", t)) if on]
        return True, "+".join(lit)
    return False, None


def lit_signals(sig_year: str, sig_runtime: str, sig_director: str, sig_title: str) -> list[str]:
    """亮起（bad）的信号名。"""
    return [n for n, v in (("Y", sig_year), ("RT", sig_runtime), ("D", sig_director), ("T", sig_title)) if v == "bad"]


def classify_shared_pair(movie_halted: bool, other_halted: bool, movie_title: str, other_title: str,
                         movie_director: str, other_director: str) -> str:
    """R19：已熔断为 false_positive_link；未熔断且两边标题相等或子集、导演不矛盾为 likely_duplicate_movie；其余 undetermined。"""
    if movie_halted or other_halted:
        return "false_positive_link"
    if (movie_title in ("ok", "sub") and other_title in ("ok", "sub")
            and movie_director in ("ok", "na") and other_director in ("ok", "na")):
        return "likely_duplicate_movie"
    return "undetermined"


# ---------------------------------------------------------------- 发行商（R45）

_PUNCT = re.compile(r"[^\w]+", re.U)


def publisher_key(raw: str) -> str:
    """发行商写法的折叠键：去标点、大小写折叠。"""
    return _SPACES.sub(" ", _PUNCT.sub(" ", fold(raw))).strip()


def publisher_root(raw: str, rules: RuleSet = DEFAULT_RULES) -> str:
    """去掉前导 The 与厂牌后缀（Home Video、Pictures、Studios、Inc. 等）后的词根；全部是后缀时保留原折叠键。"""
    key = publisher_key(raw)
    if key.startswith("the ") and len(key.split()) > 1:
        key = key[4:]
    suffixes = sorted(rules.publisher_suffixes, key=lambda s: -len(s.split()))
    changed = True
    while changed:
        changed = False
        for suf in suffixes:
            if key.endswith(" " + suf) or key == suf:
                rest = key[:-len(suf)].strip()
                if rest:
                    key = rest
                    changed = True
    return key


def publishers_compatible(a: str, b: str, rules: RuleSet = DEFAULT_RULES) -> bool:
    """词根相同，或一方是另一方的词前缀（Warner 与 Warner Archive）。"""
    ra, rb = publisher_root(a, rules).split(), publisher_root(b, rules).split()
    if not ra or not rb:
        return False
    n = min(len(ra), len(rb))
    return ra[:n] == rb[:n]


def section_rank(section: str | None, rules: RuleSet = DEFAULT_RULES) -> int:
    """R44：区域优先级 detail_bullets > product_overview > product_details > byline；未知区域最后。"""
    try:
        return rules.section_priority.index(section or "")
    except ValueError:
        return len(rules.section_priority)


@dataclass(frozen=True)
class PublisherDecision:
    status: str
    value: str | None
    conflict_type: str
    rule: str
    reason: str
    superseded: tuple[int, ...] = ()    # 与胜出值兼容但写法不同的候选下标
    rejected: tuple[int, ...] = ()


def decide_publisher(cands: Sequence[tuple[str, str | None]], rules: RuleSet = DEFAULT_RULES) -> PublisherDecision:
    """R45：cands 为 (原始值, 区域) 列表。去标点、大小写折叠与厂牌后缀后相同或存在前缀包含：取信息更完整的值
    （formatting_or_alias）；完全不同：按区域优先级取值并标 intra_asin_publisher_conflict，不兼容的候选标 rejected。"""
    if not cands:
        return PublisherDecision("missing", None, "none", "PB0", "没有发行商候选")
    keys = {publisher_key(v) for v, _ in cands}
    ranked = sorted(range(len(cands)), key=lambda k: (section_rank(cands[k][1], rules), k))
    if len(keys) == 1:
        top = ranked[0]
        return PublisherDecision("resolved", cands[top][0], "none", "PB1", "各区域发行商写法一致")
    if all(publishers_compatible(cands[ranked[0]][0], v, rules) for v, _ in cands):
        # 都与优先区域的值兼容：取信息更完整的（折叠键更长），平票取区域优先、先出现者
        best = sorted(range(len(cands)), key=lambda k: (-len(publisher_key(cands[k][0])),
                                                        section_rank(cands[k][1], rules), k))[0]
        sup = tuple(k for k in range(len(cands)) if publisher_key(cands[k][0]) != publisher_key(cands[best][0]))
        return PublisherDecision("resolved", cands[best][0], "formatting_or_alias", "PB2",
                                 "各写法去标点、大小写与厂牌后缀后相同或互为前缀，取信息更完整的写法", sup)
    top = ranked[0]
    winner = cands[top][0]
    comp = [k for k in range(len(cands)) if k != top and publishers_compatible(winner, cands[k][0], rules)
            and publisher_key(cands[k][0]) != publisher_key(winner)]
    rej = tuple(k for k in range(len(cands)) if publisher_key(cands[k][0]) != publisher_key(winner)
                and k not in comp)
    return PublisherDecision("resolved", winner, "intra_asin_publisher_conflict", "PB3",
                             "同一 ASIN 的发行商完全不同，按区域优先级取值（品牌与发行方并列时不等于前者一定正确）",
                             tuple(comp), rej)


# ---------------------------------------------------------------- 同 ASIN 数值字段与碟数（R44、R47）

def pick_by_section(items: Sequence[tuple[Any, str | None]], rules: RuleSet = DEFAULT_RULES) -> int:
    """items 为 (值, 区域)；返回区域优先级最高（同级取先出现）的下标。"""
    return min(range(len(items)), key=lambda k: (section_rank(items[k][1], rules), k))


_NUM_WORDS = {"one": 1, "single": 1, "two": 2, "double": 2, "three": 3, "triple": 3, "four": 4, "five": 5, "six": 6,
              "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_DISC_PATTERN = re.compile(r"\b(\d+|one|single|two|double|three|triple|four|five|six|seven|eight|nine|ten)"
                           r"[-\s]*(?:disc|disk)s?\b", re.I)


def discs_from_packaging(text: str | None) -> int | None:
    """R47：从包装串提取碟数：2-Disc Set、Two-Disc Special Edition、3 Discs。找不到返回 None。"""
    if not text:
        return None
    m = _DISC_PATTERN.search(text)
    if not m:
        return None
    word = m.group(1).casefold()
    return int(word) if word.isdigit() else _NUM_WORDS[word]


# ---------------------------------------------------------------- 多人串（R39）

_PERSON_SPLIT = re.compile(r"\s*;\s*(?:and\s+)?|\s+and\s+|\s*&\s*", re.I)


def split_multi_person(raw: str) -> list[str]:
    """含分隔符（;、 and 、&）的页面署名按段拆分；没有分隔符返回原串一段。不产生人物，只给对齐逐段比较用（R39）。"""
    segments = [s.strip() for s in _PERSON_SPLIT.split(raw.strip())]
    segments = [s for s in segments if s]
    return segments if len(segments) > 1 else [raw.strip()]


def is_multi_person(raw: str) -> bool:
    return len(split_multi_person(raw)) > 1


# ---------------------------------------------------------------- 署名顺序（R42）

def choose_billing_order(orders: Iterable[int]) -> tuple[int, dict[int, int]]:
    """同作品同人同角色跨版本署名顺序不一致时取出现商品数最多的序号，平票取最小序号；返回 (序号, 各序号的商品数分布)。"""
    dist = dict(sorted(Counter(orders).items()))
    top = max(dist.values())
    return min(o for o, n in dist.items() if n == top), dist


# ---------------------------------------------------------------- 类型受控词表与词面处置（R34–R35）

IMDB_GENRES = ("Action", "Adult", "Adventure", "Animation", "Biography", "Comedy", "Crime", "Documentary", "Drama",
               "Family", "Fantasy", "Film-Noir", "History", "Horror", "Music", "Musical", "Mystery", "News",
               "Romance", "Sci-Fi", "Short", "Sport", "Thriller", "War", "Western")
CONTROLLED_GENRES = (*IMDB_GENRES, "Anime", "Foreign", "Performing Arts", "LGBT", "Silent")   # R34：30 词

_GENRE_MAP: dict[str, tuple[str, ...]] = {}


def _g(genres: str | tuple[str, ...], *terms: str) -> None:
    value = (genres,) if isinstance(genres, str) else genres
    for t in terms:
        _GENRE_MAP[t] = value


_g("Drama", "drama", "tv drama", "drama classic")
_g("Comedy", "comedy", "classic comedies", "stand-up")
_g("Horror", "horror", "slasher flicks")
_g("Romance", "romance", "love & romance")
_g("Documentary", "documentary")
_g("Action", "action", "martial arts")
_g("Western", "western", "westerns")
_g("Animation", "animation", "animated")
_g("Sci-Fi", "science fiction", "sci-fi")
_g("Musical", "musical", "musicals")
_g("Thriller", "thriller", "thrillers", "suspense")
_g("Mystery", "mystery")
_g("Film-Noir", "film noir")
_g("Crime", "crime")
_g("Fantasy", "fantasy")
_g("Adventure", "adventure")
_g("Family", "kids & family", "children", "children's", "family", "tv family")
_g("War", "military & war", "war", "world war ii")
_g("Foreign", "foreign", "foreign films", "international", "art house & international", "bollywood")
_g("LGBT", "lgbt", "gay & lesbian", "gay & lesbian video")
_g("Silent", "silent", "silent films")
_g("Biography", "biography")
_g("History", "history")
_g("Sport", "sport", "sports")
_g("Music", "music", "classical", "international music", "indie & alternative", "rock", "classic rock",
   "progressive rock", "pop", "vocal pop", "rap & hip-hop", "latin music", "children's music", "r&b")
# T7-08 在开发池证据上追加（只补语义明确的同类词）：音乐风格与体裁、同义写法、非影视零售目录与地区词
_g("Music", "progressive", "hardcore & punk", "album-oriented rock (aor)", "alternative rock", "american punk", "punk",
   "punk revival", "gangsta & hardcore", "rockabilly", "oldies & retro", "blues", "delta blues", "regional blues",
   "blues - general", "folk", "british folk", "british & celtic folk", "flamenco", "latin pop", "sing-a-longs",
   "classical-music", "concertos", "symphonies", "sonatas", "sonatinas", "nocturnes", "masses",
   "opera & classical vocal", "opera & vocal", "vocal non-opera")
_g("Family", "kids and family", "family entertainment")
_g("War", "military")
_g("Short", "short")
_g("Thriller", "spy thriller")
_g(("Comedy", "Romance"), "comedy romance")
_g(("Action", "Adventure"), "action & adventure")
_g(("Mystery", "Thriller"), "mystery & thrillers", "mystery & suspense")
_g(("Sci-Fi", "Fantasy"), "science fiction & fantasy")
_g(("Comedy", "Romance"), "romantic comedy", "romantic comedies")
_g(("Animation", "Anime"), "anime", "anime & manga")
_g(("Musical", "Performing Arts"), "musicals & performing arts", "performing arts")

_GENRE_NOISE = frozenset({
    "movies & tv", "featured categories", "dvd", "blu-ray", "dvd movie", "blu-ray movie", "vhs", "movies",
    "cds & vinyl", "classic", "classics", "feature", "general", "live", "performance", "cult movies",
    "award winning", "europe", "britain", "britain & ireland", "continental europe", "digital movie", "buy movie",
    "rent movie", "prime video", "3-6 years", "7-9 years", "10-12 years", "books", "electronics", "accessories",
    "camera & photo", "video games", "music (cds)", "toys & games", "home & garden",
    "3d", "3-d", "universal blu-ray", "eastern europe", "poland", "spain", "literature & fiction", "genre fiction",
    "forms & genres", "arts, crafts & sewing", "beauty & personal care", "clothing", "clothing, shoes & jewelry",
    "diagnostics & screening", "dvd games", "games & accessories", "eyes", "eyeshadow", "makeup", "kits", "men",
    "grilling cookware & rotisseries", "grill racks", "grills & outdoor cooking", "industrial & scientific",
    "novelty", "novelty & more", "outdoor cooking tools & accessories", "painting",
    "painting, drawing & art supplies", "patio, lawn & garden", "professional medical supplies",
    "sphygmomanometers", "shirts", "t-shirts"})

_SEE_MORE = re.compile(r"\s+see more\s*$", re.I)


def clean_genre_term(raw: str) -> str:
    """页面类型词面规整（R34）：去 ` see more` 后缀，把 `_` 还原为空格，折叠空白。保留大小写以便原样展示。"""
    s = _SEE_MORE.sub("", raw.strip()).replace("_", " ")
    return _SPACES.sub(" ", s).strip()


def classify_genre_term(raw: str) -> tuple[str, str, tuple[str, ...]]:
    """页面词面 → (规整词面, 处置, 受控词)。处置 mapped / noise / unmapped（R35）：复合词拆成多个受控词；
    载体、导航与年龄段词记 noise；其余不猜测，记 unmapped，不进入 genres。"""
    term = clean_genre_term(raw)
    low = term.casefold()
    if low in _GENRE_MAP:
        return term, "mapped", _GENRE_MAP[low]
    if low in _GENRE_NOISE:
        return term, "noise", ()
    return term, "unmapped", ()


# ---------------------------------------------------------------- 选择记录的置信度

def imdb_confidence(base: float, rules: RuleSet, *, single_signal: bool = False, override: bool = False) -> float:
    """采用 IMDb 值时的置信度：关联基础可靠度，弱匹配单信号扣减，跨源覆盖不超过 conf_override。"""
    conf = base - (rules.penalty_single_signal if single_signal else 0.0)
    if override:
        conf = min(conf, rules.conf_override)
    return round(max(conf, 0.0), 2)
