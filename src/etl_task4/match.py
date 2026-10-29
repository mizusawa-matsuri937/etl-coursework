"""候选特征、得分、硬冲突与三档判定（spec R12–R15、R21–R25、R27，RULE_VERSION = t4r1）。纯函数，不访问数据库。

权重、阈值、差值与置信度换算集中在 RuleSet，随规则版本记录（design“特征、得分与判定”）；改任何默认值必须升 RULE_VERSION。
判定的三档：
- 有硬冲突（导演完全不重叠、年份相差超过 1 年、关联到不同 IMDb 作品）直接 reject；
- 得分 ≥ 自动阈值、满足 R22（有年份，且导演或演员的姓名键严格重叠）、领先第二名 ≥ 差值，才是 auto；
- 得分 ≥ 复核阈值，或本应 auto 但领先不足（并列）、R22 不满足，是 review；其余 reject。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

from . import RULE_VERSION


@dataclass(frozen=True)
class RuleSet:
    version: str = RULE_VERSION
    w_title_primary: float = 2       # G1：标题键等于主标题或原名
    w_title_alias: float = 1         # G2：只等于别名
    w_title_product: float = 2       # G3：商品之间标题键相同
    w_director_strict: float = 3
    w_director_loose: float = 1
    w_actor_two: float = 3           # 严格键重叠 ≥2
    w_actor_one: float = 2
    w_actor_disjoint: float = -2     # 两边各 ≥3 人且完全不重叠
    actor_disjoint_min: int = 3
    w_runtime_close: float = 1
    w_runtime_far: float = -1
    runtime_abs: float = 5           # 片长差 ≤ max(5 分钟, 8%) 算接近
    runtime_rel: float = 0.08
    runtime_far_rel: float = 0.25    # 差 > 25% 且无剪辑标记算相差很远
    w_year: float = 2
    year_tolerance: int = 1
    auto_threshold: float = 6
    review_threshold: float = 3
    min_margin: float = 2
    max_score: float = 11
    imdb_candidate_cap: int = 50
    block_cap: int = 30
    member_runtime_scored: bool = False   # 合集片长是总长，成员不计片长

    def thresholds(self) -> dict[str, Any]:
        return asdict(self)

    def with_version(self, version: str, **over: Any) -> "RuleSet":
        return replace(self, version=version, **over)


DEFAULT_RULES = RuleSet()

# 规则版本 → 权重与阈值。改任何默认值都要新增一个版本并登记在这里；DAG 和命令行按版本名取规则集。
RULESETS: dict[str, RuleSet] = {DEFAULT_RULES.version: DEFAULT_RULES}

# t4r2（2026-10-29，T4-07）：只看开发池的判定结果调整。导演姓名键严格重叠的权重由 3 提到 4（满分相应由 11 到 12），
# 使“标题 + 导演严格重叠”（2 + 4 = 6）达到自动阈值；开发池里这一档此前因片长缺失或差异大停在复核（5 分）。
# 领先差、自动阈值、复核阈值与其余权重不变，并列与 R22 的限制照旧。调整前后的分布见 design“规则调整记录”。
RULESETS["t4r2"] = DEFAULT_RULES.with_version("t4r2", w_director_strict=4, max_score=12)


def ruleset(version: str) -> RuleSet:
    try:
        return RULESETS[version]
    except KeyError:
        raise ValueError(f"未登记的规则版本 {version!r}（已登记：{sorted(RULESETS)}）") from None


@dataclass(frozen=True)
class PersonKey:
    strict: str
    loose: str


@dataclass(frozen=True)
class PageFacts:
    """一个商品（或合集成员）参与比较的页面事实；refs 记录各特征所用的字段断言编号。"""
    entity_id: str
    title_key: str
    title_year: int | None = None
    directors: tuple[PersonKey, ...] = ()
    actors: tuple[PersonKey, ...] = ()
    runtime: int | None = None
    cut_marker: bool = False
    genres: frozenset[str] = frozenset()
    is_member: bool = False
    refs: dict[str, tuple[int, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class ImdbFacts:
    tconst: str
    title_type: str
    start_year: int | None = None
    runtime: int | None = None
    genres: frozenset[str] = frozenset()
    directors: tuple[PersonKey, ...] = ()
    actors: tuple[PersonKey, ...] = ()
    refs: dict[str, Any] = field(default_factory=dict)   # IMDb 源记录定位：文件、行号、nconst


@dataclass
class PairScore:
    score: float
    features: dict[str, dict[str, Any]]
    veto: list[str]
    director_strict: bool
    actor_strict: int
    has_year: bool            # IMDb 一侧有 startYear（R22 的“年份”条件）
    both_title_years: bool    # G3：两边都有标题年份
    same_title_year: bool


def _overlap(a: tuple[PersonKey, ...], b: tuple[PersonKey, ...]) -> tuple[list[str], list[str]]:
    """(严格键重叠, 只有宽松键重叠)；宽松键只算弱证据。"""
    b_strict, b_loose = {y.strict for y in b}, {y.loose for y in b}
    strict = sorted({x.strict for x in a if x.strict in b_strict})
    loose = sorted({x.loose for x in a if x.strict not in b_strict and x.loose in b_loose})
    return strict, loose


def director_feature(left: tuple[PersonKey, ...], right: tuple[PersonKey, ...], r: RuleSet) -> dict[str, Any]:
    if not left or not right:
        return {"value": "missing", "score": 0, "left": len(left), "right": len(right)}
    strict, loose = _overlap(left, right)
    if strict:
        return {"value": "strict", "score": r.w_director_strict, "overlap": strict}
    if loose:
        return {"value": "loose", "score": r.w_director_loose, "overlap": loose}
    return {"value": "disjoint", "score": 0, "veto": "director_disjoint", "left": len(left), "right": len(right)}


def actor_feature(left: tuple[PersonKey, ...], right: tuple[PersonKey, ...], r: RuleSet) -> dict[str, Any]:
    strict, _ = _overlap(left, right)
    if len(strict) >= 2:
        return {"value": "overlap_2plus", "score": r.w_actor_two, "overlap": strict}
    if len(strict) == 1:
        return {"value": "overlap_1", "score": r.w_actor_one, "overlap": strict}
    if len(left) >= r.actor_disjoint_min and len(right) >= r.actor_disjoint_min:
        return {"value": "disjoint", "score": r.w_actor_disjoint, "left": len(left), "right": len(right)}
    return {"value": "none", "score": 0, "left": len(left), "right": len(right)}


def runtime_feature(a: int | None, b: int | None, cut_marker: bool, r: RuleSet, scored: bool = True) -> dict[str, Any]:
    """片长差；带剪辑标记（导演剪辑版、加长版等）的商品，差异大不算反证（R13）。"""
    if a is None or b is None:
        return {"value": "missing", "score": 0}
    diff, ref = abs(a - b), max(a, b)
    out = {"left": a, "right": b, "diff": diff}
    if not scored:
        return {**out, "value": "not_scored", "score": 0}
    if diff <= max(r.runtime_abs, r.runtime_rel * ref):
        return {**out, "value": "close", "score": r.w_runtime_close}
    if diff > r.runtime_far_rel * ref and not cut_marker:
        return {**out, "value": "far", "score": r.w_runtime_far}
    return {**out, "value": "far_with_cut_marker" if cut_marker and diff > r.runtime_far_rel * ref else "differs",
            "score": 0}


def year_feature(a: int | None, b: int | None, r: RuleSet) -> dict[str, Any]:
    if a is None or b is None:
        return {"value": "missing", "score": 0, "left": a, "right": b}
    if abs(a - b) <= r.year_tolerance:
        return {"value": "match", "score": r.w_year, "left": a, "right": b}
    return {"value": "conflict", "score": 0, "veto": "year_conflict", "left": a, "right": b}


def score_imdb(left: PageFacts, right: ImdbFacts, generator: str, r: RuleSet) -> PairScore:
    """商品（或合集成员）对 IMDb 作品。K（类型）只记录，不计分。"""
    title = {"value": generator, "score": r.w_title_primary if generator == "G1" else r.w_title_alias,
             "key": left.title_key}
    feats = {"title": title,
             "director": director_feature(left.directors, right.directors, r),
             "actor": actor_feature(left.actors, right.actors, r),
             "runtime": runtime_feature(left.runtime, right.runtime, left.cut_marker, r,
                                        scored=not left.is_member or r.member_runtime_scored),
             "year": year_feature(left.title_year, right.start_year, r),
             "genre": {"value": sorted(left.genres & right.genres), "score": 0}}
    veto = [f["veto"] for f in feats.values() if f.get("veto")]
    return PairScore(sum(f["score"] for f in feats.values()), feats, veto,
                     feats["director"]["value"] == "strict",
                     len(feats["actor"].get("overlap", [])) if feats["actor"]["score"] > 0 else 0,
                     right.start_year is not None, False, False)


def score_pair(a: PageFacts, b: PageFacts, r: RuleSet) -> PairScore:
    """商品对商品（G3）：标题键相同；年份用两边的标题年份。"""
    feats = {"title": {"value": "G3", "score": r.w_title_product, "key": a.title_key},
             "director": director_feature(a.directors, b.directors, r),
             "actor": actor_feature(a.actors, b.actors, r),
             "runtime": runtime_feature(a.runtime, b.runtime, a.cut_marker or b.cut_marker, r),
             "year": year_feature(a.title_year, b.title_year, r)}
    veto = [f["veto"] for f in feats.values() if f.get("veto")]
    both = a.title_year is not None and b.title_year is not None
    return PairScore(sum(f["score"] for f in feats.values()), feats, veto, feats["director"]["value"] == "strict",
                     len(feats["actor"].get("overlap", [])) if feats["actor"]["score"] > 0 else 0, False, both,
                     both and a.title_year == b.title_year)


@dataclass(frozen=True)
class Decision:
    tier: str                      # auto | review | reject
    score: float
    confidence: float
    rank: int | None
    margin: float | None
    veto: tuple[str, ...]
    reason: str | None
    features: dict[str, Any]
    thresholds: dict[str, Any]


def confidence(score: float, veto: list[str], r: RuleSet) -> float:
    return 0.0 if veto else round(min(max(score, 0) / r.max_score, 1), 2)


def decide_imdb_candidates(scored: list[tuple[str, PairScore]], r: RuleSet, *, capped: bool = False,
                           ) -> dict[str, Decision]:
    """同一左实体的全部 IMDb 候选（key = tconst）→ 各自的判定。名次与领先差在没有硬冲突的候选之间计算，
    并列的候选领先差为 0，所以不会自动合并（R23）。capped：候选被截断时最高只能判为 review。"""
    live = [(k, s) for k, s in scored if not s.veto]
    out: dict[str, Decision] = {}
    ranks = {k: 1 + sum(1 for _, o in live if o.score > s.score) for k, s in live}
    for key, s in scored:
        if s.veto:
            out[key] = Decision("reject", s.score, 0.0, None, None, tuple(s.veto), s.veto[0], s.features,
                                r.thresholds())
            continue
        others = [o.score for k, o in live if k != key]
        margin = s.score - max(others, default=0)
        r22 = s.has_year and (s.director_strict or s.actor_strict >= 1)
        eligible = s.score >= r.auto_threshold
        reason: str | None = None
        if eligible and r22 and margin >= r.min_margin and not capped:
            tier = "auto"
        elif s.score >= r.review_threshold:
            tier = "review"
            if capped:
                reason = "candidate_overflow"
            elif eligible and not r22:
                reason = "r22_not_met"
            elif eligible:
                reason = "tie" if margin <= 0 else "margin_below_threshold"
        else:
            tier = "reject"
        out[key] = Decision(tier, s.score, confidence(s.score, s.veto, r), ranks[key], margin, tuple(s.veto), reason,
                            s.features, r.thresholds())
    return out


def decide_pair(s: PairScore, a_link: str | None, b_link: str | None, r: RuleSet, *, oversized: bool = False,
                ) -> Decision:
    """商品对商品（R25）。a_link、b_link：两边各自自动关联的 tconst（没有为 None）。"""
    feats = dict(s.features)
    feats["links"] = {"left": a_link, "right": b_link}
    base = dict(score=s.score, rank=None, margin=None, features=feats, thresholds=r.thresholds())

    def d(tier: str, reason: str | None, veto: list[str] | None = None) -> Decision:
        v = tuple(veto if veto is not None else s.veto)
        return Decision(tier, confidence=confidence(s.score, list(v), r), veto=v, reason=reason, **base)

    if a_link and b_link:
        if a_link != b_link:
            return d("reject", "different_external_id", [*s.veto, "different_external_id"])
        return d("auto", "same_imdb_work") if not s.veto else d("reject", s.veto[0])
    if s.veto:
        return d("reject", s.veto[0])
    if a_link or b_link:
        return d("review", "suggest_join_imdb_cluster") if s.score >= r.review_threshold else d("reject", None)
    if s.same_title_year and s.director_strict and not oversized:
        return d("auto", "page_cluster")
    if s.score >= r.review_threshold:
        reason = "block_overflow" if oversized else ("no_title_year" if not s.both_title_years else None)
        return d("review", reason)
    return d("reject", None)
