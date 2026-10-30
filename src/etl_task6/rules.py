"""任务 6 的规则版本与纯函数判定（spec R14–R16、R09–R11、R21–R23，规则版本 t6r1）。不访问数据库。

RuleSet 集中所有默认值：候选置信度、择优顺序、共现评分与标题、编号条件、评论计票门槛、
B3 天数与上界置信度分档。改任何数值就是新规则版本：用 RuleSet.with_version 派生并登记到 RULESETS。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from . import COOCCUR_PARAM_VERSION, CURRENT_RULE_VERSION, MENTION_PATTERN_VERSION, RULE_VERSION

SOURCE_ORDER = ("imdb_t4_link", "imdb_t5_hint", "imdb_t6_cooccur", "amazon_title_year")
STATUS_OF_SOURCE = {"imdb_t4_link": "verified", "imdb_t5_hint": "hint_matched", "imdb_t6_cooccur": "cooccur_matched",
                    "amazon_title_year": "page_reported"}


@dataclass(frozen=True)
class RuleSet:
    version: str
    # 候选置信度（H05）
    conf_t4_link: float = 0.95
    conf_t5_hint: float = 0.80
    conf_cooccur: float = 0.75
    conf_cooccur_tiebreak: float = 0.70      # 经评论年份消歧的共现候选
    conf_title_year: float = 0.60
    # 分歧（R16）：相差超过该年数写 year_disagreement，恰好等于只计数
    year_disagreement_tolerance: int = 1
    # 人物共现反查（R09，H11）：这些值与 COOCCUR_PARAM_VERSION 一起构成 cooccur 阶段键的一部分
    cooccur_director_points: int = 2
    cooccur_actor_points: int = 1
    cooccur_min_score: int = 2
    # 评论年份提及的计票门槛（R10 的消歧用，H12）
    review_year_min_votes: int = 2
    review_year_min_share: float = 0.60
    # 评论上界条件 B3、B5（R21）
    b3_days: int = 365
    min_review_year: int = 1997
    # 上界置信度分档（design）
    conf_bound_media: float = 0.7
    conf_bound_media_jan: float = 0.6
    conf_bound_review: float = 0.6
    conf_bound_review_sparse: float = 0.4
    review_dense_min: int = 3        # 最早评论后 365 天内的评论不足这么多条时降低置信度

    def with_version(self, version: str, **over: Any) -> "RuleSet":
        return dataclasses.replace(self, version=version, **over)

    def thresholds(self) -> dict[str, Any]:
        out = {k: v for k, v in dataclasses.asdict(self).items() if k != "version"}
        out["cooccur_param_version"] = COOCCUR_PARAM_VERSION
        out["mention_pattern_version"] = MENTION_PATTERN_VERSION
        return out

    def confidence_of(self, source: str, flags: tuple[str, ...] = ()) -> float:
        if source == "imdb_t4_link":
            return self.conf_t4_link
        if source == "imdb_t5_hint":
            return self.conf_t5_hint
        if source == "imdb_t6_cooccur":
            return self.conf_cooccur_tiebreak if "review_year_tiebreak" in flags else self.conf_cooccur
        if source == "amazon_title_year":
            return self.conf_title_year
        raise ValueError(f"未知的候选来源 {source!r}")


DEFAULT_RULES = RuleSet(RULE_VERSION)
RULESETS: dict[str, RuleSet] = {DEFAULT_RULES.version: DEFAULT_RULES}
assert CURRENT_RULE_VERSION in RULESETS


def ruleset(version: str) -> RuleSet:
    try:
        return RULESETS[version]
    except KeyError:
        raise ValueError(f"未登记的规则版本 {version!r}，已登记：{sorted(RULESETS)}") from None


# ---------------------------------------------------------------- 择优与状态（R14–R15）

@dataclass(frozen=True)
class CandBrief:
    """择优时用到的候选要点。"""

    candidate_id: int
    source: str
    source_key: str
    value_year: int
    flags: tuple[str, ...] = ()
    tconst: str | None = None          # 三种 IMDb 来源的 source_key 就是 tconst


@dataclass(frozen=True)
class Choice:
    status: str                          # verified / hint_matched / cooccur_matched / page_reported / missing
    missing_reason: str | None
    adopted: CandBrief | None
    considered: tuple[CandBrief, ...]
    reason: str


def choose_candidates(cands: list[CandBrief], *, multiple_tconst_hint: bool, cooccur_ambiguous: bool) -> Choice:
    """R14–R15：同语义候选按固定顺序择优；商品标题年份只在作品内一致时可采用；没有可用候选时按原因记 missing。

    missing_reason 的优先顺序：hint_ambiguous > cooccur_ambiguous > title_years_disagree > no_candidate
    （线索自相矛盾比共现不唯一更能说明“查过但定不下”；标题年份不一致只有在确有候选时才轮到它）。"""
    considered = tuple(sorted(cands, key=lambda c: c.candidate_id))
    for source in SOURCE_ORDER:
        got = sorted((c for c in cands if c.source == source), key=lambda c: c.candidate_id)
        if not got:
            continue
        if source == "amazon_title_year":
            years = {c.value_year for c in got}
            if len(years) > 1:
                break                    # 标题年份不一致 → missing（title_years_disagree）
            adopted = got[0]
            return Choice("page_reported", None, adopted, considered,
                          f"采用商品标题年份 {adopted.value_year}（作品内 {len(got)} 个商品的标题年份一致）")
        adopted = got[0]                 # IMDb 来源在同一来源内取断言/序号最小的一条
        return Choice(STATUS_OF_SOURCE[source], None, adopted, considered, f"采用 {source} 候选（{adopted.source_key}）")
    if multiple_tconst_hint:
        reason, why = "hint_ambiguous", "线索给出多个不同的 tconst，只记录不采用（R08）"
    elif cooccur_ambiguous:
        reason, why = "cooccur_ambiguous", "人物共现反查命中多部作品且评论年份不能唯一消歧（R10）"
    elif any(c.source == "amazon_title_year" for c in cands):
        reason, why = "title_years_disagree", "商品标题年份不一致，不采用（R14）"
    else:
        reason, why = "no_candidate", "没有任何可用的年份候选（R15）"
    return Choice("missing", reason, None, considered, why)


@dataclass(frozen=True)
class Disagreement:
    year_diff_1: tuple[int, ...]           # 与采用值相差 1 年的候选 ID
    year_disagreements: tuple[dict[str, Any], ...]   # 相差超过容差的候选：candidate_id、year、diff
    tconst_disagreement: tuple[str, ...]   # 除采用 tconst 外的其他 tconst


def evaluate_disagreement(choice: Choice, rules: RuleSet) -> Disagreement:
    """R16：采用的候选与未采用的同语义候选比较；线索与共现指向不同 tconst 时记 tconst_disagreement。"""
    if choice.adopted is None:
        return Disagreement((), (), ())
    adopted, diff1, bad, tconsts = choice.adopted, [], [], set()
    imdb_sources = {"imdb_t5_hint", "imdb_t6_cooccur"}
    hint_or_cooccur = {c.source_key for c in choice.considered if c.source in imdb_sources}
    for c in choice.considered:
        if c.candidate_id == adopted.candidate_id:
            continue
        diff = abs(c.value_year - adopted.value_year)
        if diff == rules.year_disagreement_tolerance:
            diff1.append(c.candidate_id)
        elif diff > rules.year_disagreement_tolerance:
            bad.append({"candidate_id": c.candidate_id, "source": c.source, "year": c.value_year, "diff": diff})
    if adopted.source in imdb_sources:
        tconsts = {k for k in hint_or_cooccur if k != adopted.source_key}
    return Disagreement(tuple(sorted(diff1)), tuple(sorted(bad, key=lambda x: x["candidate_id"])),
                        tuple(sorted(tconsts)))


# ---------------------------------------------------------------- 共现反查的标题与编号条件（R09）

# 标题键里的冠词、介词与载体、版本词：不算实词
COOCCUR_STOPWORDS = frozenset({
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "from", "by", "with", "vhs", "dvd",
    "blu", "ray", "edition", "collection", "vol", "volume", "special", "widescreen", "full", "screen"})


def content_words(key: str) -> frozenset[str]:
    """标题键的实词集合：去掉停用词（冠词、介词与载体、版本词）后的词。"""
    return frozenset(t for t in key.split() if t not in COOCCUR_STOPWORDS)


def number_tokens(key: str) -> frozenset[int]:
    """标题键里的编号（阿拉伯数字，四位年份不算）；title_key 已把末尾罗马数字转成阿拉伯数字。"""
    return frozenset(int(t) for t in key.split() if t.isdigit() and len(t) <= 3)


def cooccur_title_ok(imdb_key: str, product_key: str) -> bool:
    """标题条件：IMDb 标题键的实词非空且全部出现在商品标题键中（容忍商品标题后面的载体、系列说明）。"""
    words = content_words(imdb_key)
    return bool(words) and words <= content_words(product_key)


def cooccur_number_guard(imdb_key: str, product_key: str) -> bool:
    """编号条件：商品标题键带续集或卷号而 IMDb 标题键没有同一组编号时排除（防止命中前一部）。"""
    return number_tokens(product_key) == number_tokens(imdb_key)


# ---------------------------------------------------------------- 评论年份提及的计票门槛（R10 消歧用）

def top_review_year(votes: dict[int, int], rules: RuleSet) -> int | None:
    """最高票年份达到门槛（票数 ≥ min_votes、占比 ≥ min_share、无并列最高）时返回该年份，否则 None。"""
    total = sum(votes.values())
    if not total:
        return None
    ranked = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))
    year, n = ranked[0]
    if n >= rules.review_year_min_votes and n / total >= rules.review_year_min_share \
            and (len(ranked) == 1 or ranked[1][1] < n):
        return year
    return None


# ---------------------------------------------------------------- 推断上界（R21–R23）

@dataclass(frozen=True)
class ReviewEvidence:
    """评论证据的输入值（全部经过 R20 计算）。"""

    earliest_time: str          # ISO 日期
    earliest_year: int
    second_time: str | None
    review_count: int
    products_review_pending: bool      # B1：edition 商品里有 review_pending
    has_edition_product: bool           # B2
    adopted_imdb_year: int | None       # B4：作品已采用的 IMDb 来源年份（verified/hint/cooccur）
    review_time_min: str                # B5：该抽取有效评论的时间下界
    review_time_max: str


def _shift(iso_time: str, days: int) -> str:
    """ISO 日期加天数（B3：第二早评论是否在最早评论后这么多天内）。"""
    return (date.fromisoformat(iso_time[:10]) + timedelta(days=days)).isoformat()


def check_review_evidence(e: ReviewEvidence, rules: RuleSet) -> dict[str, bool]:
    """B1–B5 逐条件判定（R21）。B4 在作品没有 IMDb 来源年份时视为通过；page_reported 年份不参与比较。"""
    second_year_ok = e.second_time is not None and e.second_time <= _shift(e.earliest_time, rules.b3_days)
    return {
        "B1": not e.products_review_pending,
        "B2": e.has_edition_product,
        "B3": bool(second_year_ok),
        "B4": e.adopted_imdb_year is None or e.earliest_year >= e.adopted_imdb_year,
        "B5": e.review_time_min <= e.earliest_time <= e.review_time_max
              and e.earliest_year >= rules.min_review_year,
    }


@dataclass(frozen=True)
class MediaEvidence:
    """发售日期证据的输入值。"""

    value_time: str            # 最早的 media_release 日期（ISO）
    value_year: int
    jan_first_suspect: bool    # M3：1 月 1 日占位时精度降为 year
    products_review_pending: bool   # M1
    adopted_imdb_year: int | None   # M4


def check_media_evidence(e: MediaEvidence) -> dict[str, bool]:
    """M1–M4 逐条件判定（R22）。"""
    return {
        "M1": not e.products_review_pending,
        "M2": True,            # 构造输入时只取 edition 商品的 media_release（R22）；记录条件便于报告
        "M3": True,            # 最早的一个；jan_first_suspect 的精度处理由调用方按 precision=year 保存
        "M4": e.adopted_imdb_year is None or e.value_year >= e.adopted_imdb_year,
    }


def evidence_confidence(evidence: str, checks: dict[str, bool], rules: RuleSet, *, review_dense: bool = True,
                        jan_first_suspect: bool = False) -> float:
    if evidence == "media_release":
        return rules.conf_bound_media_jan if jan_first_suspect else rules.conf_bound_media
    return rules.conf_bound_review if review_dense else rules.conf_bound_review_sparse


def merge_bounds(passed: list[tuple[str, str, str]]) -> tuple[str, str, str] | None:
    """成立证据中取较早者：比较按各自精度的最晚时刻（year 视为当年 12-31），保留原精度。
    输入是 (evidence, ISO 时间, precision)；返回选中的三元组。"""
    if not passed:
        return None

    def latest(e: tuple[str, str, str]) -> str:
        return e[1] if e[2] == "day" else e[1][:4] + "-12-31"

    return min(passed, key=latest)
