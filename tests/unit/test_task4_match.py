"""任务 4 候选特征、得分与三档判定（spec AC05 的纯规则部分，R12–R15、R20–R25）。预期按 spec 与 design 的默认值手算。"""

from __future__ import annotations

import pytest

from etl_task4.match import (DEFAULT_RULES, ImdbFacts, PageFacts, PersonKey, actor_feature, decide_imdb_candidates,
                             decide_pair, director_feature, runtime_feature, score_imdb, score_pair, year_feature)
from etl_task4.normalize import name_keys, title_key

R = DEFAULT_RULES


def pk(*names: str) -> tuple[PersonKey, ...]:
    return tuple(PersonKey(*name_keys(n)) for n in names)


CASABLANCA = ImdbFacts("tt0000001", "movie", 1942, 102, frozenset({"drama"}), pk("Michael Curtiz"),
                       pk("Humphrey Bogart", "Ingrid Bergman", "Paul Henreid"))


def page(entity="A", key="casablanca", year=None, directors=("Michael Curtiz",), actors=("Humphrey Bogart",),
         runtime=102, cut=False, member=False) -> PageFacts:
    return PageFacts(entity, key, year, pk(*directors), pk(*actors), runtime, cut, is_member=member)


def decide_one(p: PageFacts, w: ImdbFacts, generator="G1", **kw):
    return decide_imdb_candidates([(w.tconst, score_imdb(p, w, generator, R))], R, **kw)[w.tconst]


# ---------------------------------------------------------------- AC05：商品对 IMDb 作品

def test_decision_same_work_three_editions_auto_and_extended_cut_not_penalised():
    dvd = decide_one(page("DVD"), CASABLANCA)
    bluray = decide_one(page("BD", runtime=102), CASABLANCA)
    extended = decide_one(page("EXT", runtime=143, cut=True), CASABLANCA)       # 比原片长 40%
    assert [d.tier for d in (dvd, bluray, extended)] == ["auto", "auto", "auto"]
    assert extended.features["runtime"]["value"] == "far_with_cut_marker" and extended.features["runtime"]["score"] == 0
    # 没有剪辑标记的同样差异扣分，但标题 2 + 导演 3 + 演员 2 = 7 仍是 auto
    far = decide_one(page("LONG", runtime=143), CASABLANCA)
    assert far.features["runtime"]["score"] == -1 and far.score == 6 and far.tier == "auto"


def test_decision_exact_scores_and_confidence_follow_the_design_table():
    d = decide_one(page(), CASABLANCA)
    assert d.score == 2 + 3 + 2 + 1 and d.confidence == round(8 / 11, 2) and d.margin == 8 and d.rank == 1
    assert d.thresholds["auto_threshold"] == 6 and d.thresholds["version"] == "t4r1"
    assert set(d.features) == {"title", "director", "actor", "runtime", "year", "genre"}
    # 标题 + 导演 + 片长 = 6 → auto（导演满足 R22）
    assert decide_one(page(actors=()), CASABLANCA).tier == "auto"
    # 没有导演字段，标题 + 2 位演员 + 片长 = 6 → auto
    assert decide_one(page(directors=(), actors=("Humphrey Bogart", "Ingrid Bergman")), CASABLANCA).tier == "auto"
    # 标题 + 1 位演员 + 片长 = 5 → review；标题 + 片长 = 3 → review；只有标题 = 2 → reject
    assert decide_one(page(directors=()), CASABLANCA).tier == "review"
    assert decide_one(page(directors=(), actors=()), CASABLANCA).tier == "review"
    assert decide_one(page(directors=(), actors=(), runtime=None), CASABLANCA).tier == "reject"


def test_decision_title_only_without_people_or_year_is_never_auto():
    w = ImdbFacts("tt0000009", "movie", None, None)
    d = decide_one(PageFacts("A", "casablanca"), w)
    assert d.tier == "reject" and d.score == 2
    d = decide_one(PageFacts("A", "casablanca", runtime=100), ImdbFacts("tt0000009", "movie", 1942, 100))
    assert d.tier == "review" and d.score == 3          # 仅标题 + 片长：不能自动


def test_decision_year_missing_or_same_year_alone_cannot_auto_merge():
    w = ImdbFacts("tt1", "movie", None, 102, directors=pk("Michael Curtiz"))
    d = decide_one(page(), w)                           # IMDb 没有 startYear：即使导演一致也满足不了 R22
    assert d.tier == "review" and d.reason == "r22_not_met" and d.score == 6
    same_year_only = decide_one(PageFacts("A", "casablanca", 1942), ImdbFacts("tt2", "movie", 1942))
    assert same_year_only.tier == "review" and same_year_only.score == 4   # 标题 + 年份最多复核，绝不自动


def test_decision_remake_with_different_director_and_year_is_vetoed():
    w1983 = ImdbFacts("tt0086", "movie", 1983, 100, directors=pk("Jim McBride"), actors=pk("Richard Gere"))
    p1960 = page("B1960", "breathless", 1960, ("Jean-Luc Godard",), ("Jean Seberg",), 90)
    d = decide_one(p1960, w1983)
    assert d.tier == "reject" and set(d.veto) == {"director_disjoint", "year_conflict"} and d.confidence == 0
    # 导演不重叠本身就是硬冲突，哪怕标题和年份都吻合
    d2 = decide_one(page(year=1942), ImdbFacts("tt3", "movie", 1942, 102, directors=pk("Someone Else")))
    assert d2.tier == "reject" and d2.veto == ("director_disjoint",)


def test_decision_title_year_off_by_one_is_allowed_but_two_is_a_conflict():
    w = ImdbFacts("tt4", "movie", 1943, 102, directors=pk("Michael Curtiz"))
    assert decide_one(page(year=1942), w).tier == "auto"
    assert decide_one(page(year=1941), w).veto == ("year_conflict",)


def test_decision_tie_between_two_imdb_works_goes_to_review():
    a = ImdbFacts("tt10", "movie", 1990, 100, directors=pk("Pat Roe"), actors=pk("Ann Lee"))
    b = ImdbFacts("tt11", "movie", 1991, 100, directors=pk("Pat Roe"), actors=pk("Ann Lee"))
    p = page("T", "twin", None, ("Pat Roe",), ("Ann Lee",), 100)
    out = decide_imdb_candidates([("tt10", score_imdb(p, a, "G1", R)), ("tt11", score_imdb(p, b, "G1", R))], R)
    assert {k: (v.tier, v.reason, v.margin, v.rank) for k, v in out.items()} == {
        "tt10": ("review", "tie", 0, 1), "tt11": ("review", "tie", 0, 1)}


def test_decision_clear_winner_auto_and_runner_up_follows_threshold():
    a = ImdbFacts("tt10", "movie", 1990, 100, directors=pk("Pat Roe"), actors=pk("Ann Lee"))
    b = ImdbFacts("tt11", "movie", 1991, 140, directors=pk("Pat Roe"))
    p = page("W", "twin", None, ("Pat Roe",), ("Ann Lee",), 100)
    out = decide_imdb_candidates([("tt10", score_imdb(p, a, "G1", R)), ("tt11", score_imdb(p, b, "G2", R))], R)
    assert out["tt10"].tier == "auto" and out["tt10"].margin >= 2 and out["tt10"].rank == 1
    assert out["tt11"].tier == "review" and out["tt11"].rank == 2 and out["tt11"].margin < 0


def test_decision_capped_candidates_never_auto():
    d = decide_one(page(), CASABLANCA, capped=True)
    assert d.tier == "review" and d.reason == "candidate_overflow"


def test_decision_alias_match_scores_one_point_less_than_primary_title():
    g1, g2 = decide_one(page(), CASABLANCA, "G1"), decide_one(page(), CASABLANCA, "G2")
    assert g1.score - g2.score == 1 and g2.tier == "auto"


def test_decision_collection_member_uses_page_people_and_ignores_runtime():
    member = page("COL#1", "casablanca", runtime=300, member=True)
    d = decide_one(member, CASABLANCA)
    assert d.features["runtime"]["value"] == "not_scored" and d.tier == "auto"


# ---------------------------------------------------------------- AC05：商品对商品

def test_decision_pair_cases():
    a = page("A", "cinderella", None, ("Clyde Geronimi",), ("Ilene Woods", "Eleanor Audley", "Verna Felton"))
    b = page("B", "cinderella", None, (), ("Brandy", "Whitney Houston", "Bernadette Peters"), None)
    s = score_pair(a, b, R)                              # 真人版与动画版：一边无导演，演员不重叠
    d = decide_pair(s, None, None, R)
    assert d.tier == "reject" and d.score == 0 and "actor" in d.features

    c = page("C", "meet", None, ("Robert Wise",), (), 120)
    e = page("E", "meet", None, ("Robert Wise",), (), 120)
    d = decide_pair(score_pair(c, e, R), None, None, R)  # 无标题年份：导演一致也只能复核
    assert (d.tier, d.reason) == ("review", "no_title_year")

    c2, e2 = page("C", "meet", 1941, ("Robert Wise",), (), 120), page("E", "meet", 1941, ("Robert Wise",), (), 122)
    d = decide_pair(score_pair(c2, e2, R), None, None, R)  # 标题年份相同、导演一致：自动合并
    assert (d.tier, d.reason) == ("auto", "page_cluster") and d.score == 2 + 3 + 1 + 2

    only_title = decide_pair(score_pair(page("X", "k", None, (), (), None), page("Y", "k", None, (), (), None), R),
                             None, None, R)
    assert only_title.tier == "reject" and only_title.score == 2


def test_decision_pair_hard_conflicts_and_imdb_links():
    old = page("P60", "breathless", 1960, ("Jean-Luc Godard",), (), 90)
    new = page("P83", "breathless", 1983, ("Jim McBride",), (), 100)
    d = decide_pair(score_pair(old, new, R), None, None, R)
    assert d.tier == "reject" and set(d.veto) == {"director_disjoint", "year_conflict"}
    same = page("S1", "k", 1950, ("A B",), (), 100), page("S2", "k", 1950, ("A B",), (), 100)
    s = score_pair(*same, R)
    assert decide_pair(s, "tt1", "tt2", R).reason == "different_external_id"
    assert decide_pair(s, "tt1", "tt2", R).tier == "reject"
    assert decide_pair(s, "tt1", "tt1", R).tier == "auto"        # 同一 IMDb 作品
    d = decide_pair(s, "tt1", None, R)
    assert (d.tier, d.reason) == ("review", "suggest_join_imdb_cluster")
    assert decide_pair(s, None, None, R, oversized=True).tier == "review"


def test_decision_language_versions_of_same_work_both_auto_and_pair_is_same_work():
    en, es = page("EN"), page("ES")
    link_en, link_es = decide_one(en, CASABLANCA), decide_one(es, CASABLANCA)
    assert (link_en.tier, link_es.tier) == ("auto", "auto")
    assert decide_pair(score_pair(en, es, R), "tt0000001", "tt0000001", R).reason == "same_imdb_work"


def test_decision_rocky_and_rocky_ii_never_share_a_candidate_block():
    assert title_key("Rocky") != title_key("Rocky II [VHS]".split(" [")[0])


# ---------------------------------------------------------------- 特征

def test_feature_director_strict_loose_missing_disjoint():
    assert director_feature(pk("Hamilton Luske"), pk("Hamilton Luske"), R)["value"] == "strict"
    f = director_feature(pk("Hamilton S. Luske"), pk("Hamilton Luske"), R)
    assert (f["value"], f["score"]) == ("loose", 1)
    assert director_feature((), pk("A B"), R)["value"] == "missing"
    assert director_feature(pk("A B"), pk("C D"), R)["veto"] == "director_disjoint"


def test_feature_actor_overlap_levels_and_disjoint_penalty():
    people = pk("A One", "B Two", "C Three")
    assert actor_feature(people, pk("A One", "B Two"), R)["score"] == 3
    assert actor_feature(people, pk("A One", "X Y"), R)["score"] == 2
    assert actor_feature(people, pk("D Four", "E Five", "F Six"), R)["score"] == -2
    assert actor_feature(pk("A One"), pk("D Four", "E Five", "F Six"), R)["score"] == 0   # 页面人少不算反证


def test_feature_runtime_thresholds():
    assert runtime_feature(100, 105, False, R)["value"] == "close"
    assert runtime_feature(100, 112, False, R)["value"] == "differs"
    assert runtime_feature(100, 140, False, R)["score"] == -1
    assert runtime_feature(100, 140, True, R)["score"] == 0
    assert runtime_feature(None, 100, False, R)["value"] == "missing"


def test_feature_year_conflict_is_a_veto():
    assert year_feature(1960, 1983, R)["veto"] == "year_conflict"
    assert year_feature(None, 1983, R)["score"] == 0
