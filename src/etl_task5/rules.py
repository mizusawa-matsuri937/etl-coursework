"""任务 5 的规则版本与纯函数判定（spec R11–R16、R19–R23、R25，规则版本 t5r1）。不访问数据库。

RuleSet 集中所有默认值（兼容等级置信度、年份与年代容差、跨作品分值、合作者佐证上限、块上限、迭代上限、稀有度与发行商开关）。
改任何数值或开关就是新规则版本：用 RuleSet.with_version 派生并登记到 RULESETS。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import CURRENT_RULE_VERSION, RULE_VERSION


@dataclass(frozen=True)
class RuleSet:
    version: str
    # 途径一：同作品内的兼容等级 → 置信度
    conf_x1: float = 0.95
    conf_x2: float = 0.90
    conf_x3: float = 0.85
    # 途径二：作品年表反查
    conf_filmography: float = 0.85
    conf_filmography_alias: float = 0.80          # 标题键只命中别名
    year_tolerance: int = 1                       # IMDb 年份与商品标题年份最多相差几年才能自动关联
    era_tolerance: int = 5                        # 出生后 5 年内、去世后 5 年外的作品年份视为年代冲突
    # 跨作品得分
    score_n1: int = 3
    score_n2: int = 2
    score_n3: int = 1
    score_n4: int = 1
    score_role: int = 1
    w_cluster: int = 2                            # 当前同一人物的共同合作者每位
    w_same_name: int = 1                          # 同严格键、尚未合成同一人物的合作者每位
    w_max: int = 4
    auto_w_min: int = 2
    block_limit: int = 50
    c4_block_limit: int = 200
    max_rounds: int = 10
    use_rarity: bool = False
    use_publisher: bool = False
    rare_page_works: int = 3
    rare_imdb_people: int = 1

    @property
    def max_score(self) -> int:
        return self.score_n1 + self.score_role + self.w_max

    def with_version(self, version: str, **over: Any) -> "RuleSet":
        return dataclasses.replace(self, version=version, **over)

    def thresholds(self) -> dict[str, Any]:
        return {k: v for k, v in dataclasses.asdict(self).items() if k != "version"}

    def credit_confidence(self, x_level: str) -> float:
        return {"X1": self.conf_x1, "X2": self.conf_x2, "X3": self.conf_x3}.get(x_level, 0.0)


DEFAULT_RULES = RuleSet(RULE_VERSION)
RULESETS: dict[str, RuleSet] = {DEFAULT_RULES.version: DEFAULT_RULES}
assert CURRENT_RULE_VERSION in RULESETS


def ruleset(version: str) -> RuleSet:
    try:
        return RULESETS[version]
    except KeyError:
        raise ValueError(f"未登记的规则版本 {version!r}，已登记：{sorted(RULESETS)}") from None


# ---------------------------------------------------------------- 外部 ID 途径一（R12）

@dataclass(frozen=True)
class CreditCand:
    tconst: str
    nconst: str
    x_level: str          # X1–X4
    same_role: bool       # 演职员的类别与提及的角色类别相同


@dataclass(frozen=True)
class Verdict:
    tier: str             # auto、review、reject
    reason: str
    confidence: float


def decide_credit_links(cands: Iterable[CreditCand], rules: RuleSet) -> dict[tuple[str, str], Verdict]:
    """一条提及在其（一个或多个成员）作品演职员里的候选 → 每个候选的判定，键为 (tconst, nconst)。

    单作品：同角色里恰一位 X1–X3 且没有其他 X1–X4 → auto；两位及以上 → review（multiple_in_credits）；只有 X4 → review（near_name）；
    只在另一角色类别兼容 → review（cross_role）。合集：只有一个成员给出 auto、其他成员没有任何候选时才 auto，否则都改为 review。"""
    by_work: dict[str, list[CreditCand]] = {}
    for c in cands:
        by_work.setdefault(c.tconst, []).append(c)
    out: dict[tuple[str, str], Verdict] = {}
    for tconst, group in by_work.items():
        same = [c for c in group if c.same_role]
        strong = [c for c in same if c.x_level != "X4"]
        for c in group:
            if not same:
                v = Verdict("review", "cross_role", 0.0)
            elif len(same) >= 2:
                v = Verdict("review", "multiple_in_credits", 0.0)
            elif not strong:
                v = Verdict("review", "near_name", 0.0)
            else:
                v = Verdict("auto", "unique_in_credits", rules.credit_confidence(c.x_level))
            if c.same_role or not same:
                out[(c.tconst, c.nconst)] = v
    autos = [k for k, v in out.items() if v.tier == "auto"]
    if autos and len(by_work) > 1:
        for k in autos:
            out[k] = Verdict("review", "multiple_members", 0.0)
    return out


# ---------------------------------------------------------------- 外部 ID 途径二（R14、R15）

@dataclass(frozen=True)
class FilmHit:
    nconst: str
    tconst: str
    member: int                   # 合集成员序号，电影类商品为 0
    key_source: str              # primary、original、aka
    start_year: int | None
    birth_year: int | None
    death_year: int | None
    disambiguator_conflict: bool = False


def era_conflict(birth: int | None, death: int | None, year: int | None, rules: RuleSet) -> bool:
    """出生、去世年份与作品年份不相容（R20）：作品早于出生后 era_tolerance 年，或晚于去世后 era_tolerance 年。"""
    if year is None:
        return False
    if birth is not None and year < birth + rules.era_tolerance:
        return True
    return death is not None and year > death + rules.era_tolerance


def decide_filmography(hits: Iterable[FilmHit], title_year: int | None,
                       rules: RuleSet) -> dict[tuple[str, str, int], Verdict]:
    """已核实人物的年表里与商品（或合集成员）标题键相同的作品 → 判定，键为 (nconst, tconst, 成员序号)。
    硬冲突（年代、消歧段）→ reject；其余恰一对 → auto（年份相差超过容差改为 review）；多对 → review。"""
    out: dict[tuple[str, str, int], Verdict] = {}
    valid: list[FilmHit] = []
    for h in hits:
        key = (h.nconst, h.tconst, h.member)
        if era_conflict(h.birth_year, h.death_year, h.start_year, rules):
            out[key] = Verdict("reject", "era_conflict", 0.0)
        elif h.disambiguator_conflict:
            out[key] = Verdict("reject", "different_disambiguator", 0.0)
        else:
            valid.append(h)
    for h in valid:
        key = (h.nconst, h.tconst, h.member)
        if len(valid) >= 2:
            out[key] = Verdict("review", "filmography_ambiguous", 0.0)
        elif (title_year is not None and h.start_year is not None
              and abs(title_year - h.start_year) > rules.year_tolerance):
            out[key] = Verdict("review", "filmography_year_conflict", 0.0)
        else:
            conf = rules.conf_filmography_alias if h.key_source == "aka" else rules.conf_filmography
            out[key] = Verdict("auto", "filmography_title_match", conf)
    return out


# ---------------------------------------------------------------- 跨作品（R17、R19–R22）

@dataclass(frozen=True)
class PairInput:
    n_level: str | None            # N1–N4
    roles_overlap: bool
    nconst_a: str | None = None
    nconst_b: str | None = None
    collab_cluster: int = 0        # 当前同一人物的共同合作者数
    collab_same_name: int = 0      # 同严格键（至少两个词）、尚未合成同一人物的共同合作者数
    era_conflict: bool = False
    disamb_conflict: bool = False
    rare_both: bool = False
    publisher_overlap: bool = False
    overflow: bool = False


@dataclass
class PairVerdict:
    tier: str
    score: int
    confidence: float
    veto: list[str]
    reason: str
    w: int
    tier_if_enabled: dict[str, str] = field(default_factory=dict)


def _name_score(n_level: str | None, rules: RuleSet) -> int:
    return {"N1": rules.score_n1, "N2": rules.score_n2, "N3": rules.score_n3, "N4": rules.score_n4}.get(n_level or "", 0)


def decide_pair(p: PairInput, rules: RuleSet, *, _switches: bool = True) -> PairVerdict:
    """两个不同作品单位的人物组 → 三档判定（design“跨作品候选与判定”）。有硬冲突 reject；N1 + 角色交集 + W ≥ 2 才 auto；
    稀有度与发行商开关关闭时照算，并在 tier_if_enabled 里记打开后会得到的判定档。"""
    veto = []
    if p.nconst_a and p.nconst_b and p.nconst_a != p.nconst_b:
        veto.append("different_external_id")
    if p.era_conflict:
        veto.append("era_conflict")
    if p.disamb_conflict:
        veto.append("different_disambiguator")
    use_pub = rules.use_publisher
    w = min(rules.w_max, rules.w_cluster * p.collab_cluster + rules.w_same_name * p.collab_same_name
            + (1 if use_pub and p.publisher_overlap else 0))
    score = _name_score(p.n_level, rules) + (rules.score_role if p.roles_overlap else 0) + w
    if veto:
        v = PairVerdict("reject", score, 0.0, veto, veto[0], w)
    elif p.n_level == "N1":
        if p.roles_overlap and w >= rules.auto_w_min:
            v = PairVerdict("auto", score, 0.0, [], "co_credit", w)
        elif (rules.use_rarity and p.roles_overlap and w == 1 and p.collab_same_name >= 1 and p.rare_both):
            v = PairVerdict("auto", score, 0.0, [], "rare_name_co_credit", w)
        else:
            v = PairVerdict("review", score, 0.0, [], "same_name_no_corroboration" if p.roles_overlap
                            else "role_differs", w)
    elif p.n_level == "N2":
        v = PairVerdict("review", score, 0.0, [], "name_order_or_middle", w)
    elif p.n_level in ("N3", "N4"):
        v = PairVerdict("review" if w >= 1 else "reject", score, 0.0, [], "weak_name_only", w)
    else:
        v = PairVerdict("reject", score, 0.0, [], "incompatible_name", w)
    if p.overflow and v.tier == "auto":
        v.tier, v.reason = "review", "block_overflow"
    v.confidence = 0.0 if v.veto else round(min(score, rules.max_score) / rules.max_score, 2)
    if _switches:
        v.tier_if_enabled = {
            "rarity": decide_pair(p, rules.with_version(rules.version, use_rarity=True), _switches=False).tier,
            "publisher": decide_pair(p, rules.with_version(rules.version, use_publisher=True), _switches=False).tier}
    return v


def is_rare(page_works: int, imdb_people: int, rules: RuleSet) -> bool:
    return page_works <= rules.rare_page_works and imdb_people <= rules.rare_imdb_people
