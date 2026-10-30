"""任务 5 规则层的纯函数（spec R12、R14–R16、R19–R22，AC05、AC06 的规则层部分）。预期值手写。"""

from __future__ import annotations

import pytest

from etl_task5.rules import (CreditCand, DEFAULT_RULES, FilmHit, PairInput, RuleSet, decide_credit_links,
                             decide_filmography, decide_pair, era_conflict, is_rare, ruleset)

R = DEFAULT_RULES


def test_ruleset_defaults_are_registered_and_unknown_versions_fail():
    assert ruleset("t5r1") is R and R.version == "t5r1"
    assert (R.conf_x1, R.conf_x2, R.conf_x3, R.conf_filmography, R.conf_filmography_alias) == (0.95, 0.90, 0.85, 0.85, 0.80)
    assert (R.year_tolerance, R.era_tolerance, R.w_max, R.block_limit, R.c4_block_limit, R.max_rounds) == (1, 5, 4, 50, 200, 10)
    assert not R.use_rarity and not R.use_publisher and R.max_score == 8
    assert R.with_version("t5rx", use_rarity=True).use_rarity and R.use_rarity is False
    with pytest.raises(ValueError, match="未登记"):
        ruleset("t5r99")


def test_credit_links_unique_x3_is_auto_and_x4_or_two_people_are_review():
    one = decide_credit_links([CreditCand("tt1", "nm1", "X3", True)], R)
    assert one[("tt1", "nm1")].tier == "auto" and one[("tt1", "nm1")].confidence == 0.85
    near = decide_credit_links([CreditCand("tt1", "nm1", "X4", True)], R)
    assert (near[("tt1", "nm1")].tier, near[("tt1", "nm1")].reason) == ("review", "near_name")
    two = decide_credit_links([CreditCand("tt1", "nm1", "X1", True), CreditCand("tt1", "nm2", "X1", True)], R)
    assert {v.reason for v in two.values()} == {"multiple_in_credits"} and {v.tier for v in two.values()} == {"review"}
    mixed = decide_credit_links([CreditCand("tt1", "nm1", "X1", True), CreditCand("tt1", "nm2", "X4", True)], R)
    assert {v.tier for v in mixed.values()} == {"review"}                       # 有其他 X1–X4 时不能自动
    cross = decide_credit_links([CreditCand("tt1", "nm1", "X1", False)], R)
    assert (cross[("tt1", "nm1")].tier, cross[("tt1", "nm1")].reason) == ("review", "cross_role")
    ignored = decide_credit_links([CreditCand("tt1", "nm1", "X1", True), CreditCand("tt1", "nm2", "X1", False)], R)
    assert list(ignored) == [("tt1", "nm1")] and ignored[("tt1", "nm1")].tier == "auto"


def test_credit_links_collection_needs_a_single_member_with_candidates():
    one_member = decide_credit_links([CreditCand("tt1", "nm1", "X1", True)], R)
    assert one_member[("tt1", "nm1")].tier == "auto"
    both = decide_credit_links([CreditCand("tt1", "nm1", "X1", True), CreditCand("tt2", "nm9", "X1", True)], R)
    assert {v.tier for v in both.values()} == {"review"} and {v.reason for v in both.values()} == {"multiple_members"}


def test_filmography_single_hit_auto_alias_lower_and_year_or_era_checks():
    hit = FilmHit("nm1", "tt1", 0, "primary", 1985, 1950, None)
    assert decide_filmography([hit], 1986, R)[("nm1", "tt1", 0)].confidence == 0.85
    alias = FilmHit("nm1", "tt1", 0, "aka", 1985, 1950, None)
    assert decide_filmography([alias], None, R)[("nm1", "tt1", 0)].confidence == 0.80
    far = decide_filmography([hit], 1988, R)[("nm1", "tt1", 0)]
    assert (far.tier, far.reason) == ("review", "filmography_year_conflict")
    old = FilmHit("nm1", "tt1", 0, "primary", 1952, 1950, None)                     # 出生后 2 年的作品
    assert decide_filmography([old], None, R)[("nm1", "tt1", 0)].tier == "reject"
    dead = FilmHit("nm1", "tt1", 0, "primary", 2000, 1900, 1950)
    assert decide_filmography([dead], None, R)[("nm1", "tt1", 0)].reason == "era_conflict"
    posthumous = FilmHit("nm1", "tt1", 0, "primary", 1954, 1900, 1950)              # 身后 5 年内可容纳
    assert decide_filmography([posthumous], None, R)[("nm1", "tt1", 0)].tier == "auto"
    clash = FilmHit("nm1", "tt1", 0, "primary", 1985, 1950, None, disambiguator_conflict=True)
    assert decide_filmography([clash], None, R)[("nm1", "tt1", 0)].reason == "different_disambiguator"


def test_filmography_two_hits_are_review_but_a_rejected_one_does_not_count():
    a, b = FilmHit("nm1", "tt1", 0, "primary", 1985, 1950, None), FilmHit("nm1", "tt2", 0, "primary", 1990, 1950, None)
    out = decide_filmography([a, b], None, R)
    assert {v.tier for v in out.values()} == {"review"} and {v.reason for v in out.values()} == {"filmography_ambiguous"}
    c = FilmHit("nm1", "tt3", 0, "primary", 1951, 1950, None)                       # 年代冲突，被剔除
    out = decide_filmography([a, c], None, R)
    assert out[("nm1", "tt1", 0)].tier == "auto" and out[("nm1", "tt3", 0)].tier == "reject"


def test_era_conflict_rule():
    assert era_conflict(1980, None, 1950, R) and not era_conflict(1980, None, 1990, R)
    assert era_conflict(1980, None, 1984, R) and not era_conflict(1980, None, 1985, R)
    assert era_conflict(1900, 1950, 1956, R) and not era_conflict(1900, 1950, 1955, R)
    assert not era_conflict(None, None, 1950, R) and not era_conflict(1980, None, None, R)


# ---------------------------------------------------------------- 跨作品（AC05、AC06 的规则层）

def pair(**kw):
    return decide_pair(PairInput(**{"n_level": "N1", "roles_overlap": True, **kw}), R)


@pytest.mark.parametrize("inp, tier, reason, score", [
    (dict(collab_cluster=1), "auto", "co_credit", 3 + 1 + 2),                      # 同名、一位已核实共同合作者
    (dict(collab_same_name=2), "auto", "co_credit", 3 + 1 + 2),                    # 两位同名合作者
    (dict(collab_same_name=1), "review", "same_name_no_corroboration", 5),        # 一位同名合作者不够
    (dict(), "review", "same_name_no_corroboration", 4),                          # 无合作者：姓名相同不合并
    (dict(collab_cluster=3), "auto", "co_credit", 8),                             # 上限 4
    (dict(nconst_a="nm1", nconst_b="nm2", collab_cluster=2), "reject", "different_external_id", 8),
    (dict(era_conflict=True, collab_cluster=2), "reject", "era_conflict", 8),
    (dict(disamb_conflict=True), "reject", "different_disambiguator", 4),
    (dict(collab_cluster=2, overflow=True), "review", "block_overflow", 8),
    (dict(roles_overlap=False, collab_cluster=2), "review", "role_differs", 7),
    (dict(n_level="N2", collab_cluster=2), "review", "name_order_or_middle", 2 + 1 + 4),
    (dict(n_level="N3"), "reject", "weak_name_only", 2),                          # “J. Smith” 对 “John Smith”
    (dict(n_level="N3", collab_cluster=1), "review", "weak_name_only", 1 + 1 + 2),
    (dict(n_level="N4"), "reject", "weak_name_only", 2),
    (dict(n_level=None), "reject", "incompatible_name", 1),
])
def test_ac05_pair_decision_table(inp, tier, reason, score):
    v = pair(**inp)
    assert (v.tier, v.reason, v.score) == (tier, reason, score)
    assert v.confidence == (0.0 if v.veto else round(min(score, 8) / 8, 2))


def test_ac06_switches_off_keep_tiers_and_record_what_would_change():
    rare = pair(collab_same_name=1, rare_both=True)
    common = pair(collab_same_name=1, rare_both=False)
    no_collab = pair(rare_both=True)
    publisher = pair(collab_same_name=1, publisher_overlap=True)
    assert [v.tier for v in (rare, common, no_collab, publisher)] == ["review"] * 4          # 开关关闭：与无特征时一致
    assert rare.tier_if_enabled == {"rarity": "auto", "publisher": "review"}
    assert common.tier_if_enabled == {"rarity": "review", "publisher": "review"}
    assert no_collab.tier_if_enabled == {"rarity": "review", "publisher": "review"}        # 稀有名没有合作者：不起作用
    assert publisher.tier_if_enabled == {"rarity": "review", "publisher": "auto"}


def test_ac06_switches_on():
    on_rarity = RuleSet("t5rx", use_rarity=True)
    on_pub = RuleSet("t5ry", use_publisher=True)
    p = lambda rules, **kw: decide_pair(PairInput("N1", True, **kw), rules)
    assert p(on_rarity, collab_same_name=1, rare_both=True).tier == "auto"
    assert p(on_rarity, collab_same_name=1, rare_both=True).reason == "rare_name_co_credit"
    assert p(on_rarity, collab_same_name=1, rare_both=False).tier == "review"              # 常见名不升
    assert p(on_rarity, rare_both=True).tier == "review"                                    # 无合作者不升
    assert p(on_rarity, collab_same_name=1, rare_both=True, nconst_a="a", nconst_b="b").tier == "reject"
    assert p(on_pub, publisher_overlap=True).tier == "review"                               # 发行商单独不能升
    assert p(on_pub, publisher_overlap=True).w == 1 and p(on_pub, publisher_overlap=True).score == 5
    assert p(on_pub, collab_same_name=1, publisher_overlap=True).w == 2


def test_is_rare_thresholds():
    assert is_rare(3, 1, R) and not is_rare(4, 0, R) and not is_rare(1, 2, R)
