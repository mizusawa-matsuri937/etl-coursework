"""任务 7 多值字段的纯函数：署名对齐（R38）与覆盖范围（R43）。数据库相关行为见 tests/integration/test_task7_movies_multivalue.py。"""

from __future__ import annotations

import pytest

from etl_task7.movies_multivalue import align_name, cast_coverage
from etl_task7.link_assess import director_signal
from etl_task7.rules import DEFAULT_RULES, ruleset

LEVELS = DEFAULT_RULES.align_levels             # t7r1
LEVELS2 = ruleset("t7r2").align_levels           # t7r2、t7r3：末尾加 LS


def cand(nconst: str, name: str | None, ordinal: int = 1) -> dict:
    return {"nconst": nconst, "name": name, "ordinal": ordinal}


def test_ac22_strict_match_wins_over_near_match():
    a = align_name(["John Cusack"], [cand("nm1", "Joan Cusack"), cand("nm2", "John Cusack")], LEVELS)
    assert (a.outcome, a.nconst, a.level) == ("aligned", "nm2", "L1")


def test_ac22_same_level_twice_is_a_tie_not_an_alignment():
    a = align_name(["Jon Smith"], [cand("nm1", "John Smith"), cand("nm2", "Joan Smith")], LEVELS)
    assert a.outcome == "tie_not_aligned" and a.nconst is None and a.tied == ("nm1", "nm2") and a.level == "L4"


def test_ac22_misspelling_aligns_at_the_near_level_and_unrelated_names_do_not():
    assert align_name(["Garey Busey"], [cand("nm1", "Gary Busey")], LEVELS).level == "L4"
    assert align_name(["John Doe"], [cand("nm1", "Jane Roe")], LEVELS).outcome == "no_match"
    assert align_name(["John Doe"], [cand("nm1", None)], LEVELS).outcome == "no_match"


def test_ac22_best_level_over_all_page_spellings_is_used():
    a = align_name(["Garey Busey", "Gary Busey"], [cand("nm1", "Gary Busey")], LEVELS)
    assert a.level == "L1"


@pytest.mark.parametrize("page,imdb,expected", [
    (True, True, "page_and_imdb_principals"), (True, False, "page_only"),
    (False, True, "imdb_principals_only"), (False, False, "none")])
def test_ac08_cast_coverage_values(page, imdb, expected):
    assert cast_coverage(page, imdb) == expected


# ------------------------------------------------------------------ t7r2 姓名兼容放宽（R16、R38、R41）

def test_ac22_t7r2_aligns_format_only_and_subset_names_that_t7r1_cannot():
    a = align_name(["J.P.Dutta"], [cand("nm1", "J.P. Dutta")], LEVELS2, True)
    assert (a.outcome, a.nconst, a.level) == ("aligned", "nm1", "L1")
    assert align_name(["J.P.Dutta"], [cand("nm1", "J.P. Dutta")], LEVELS).outcome == "no_match"          # 修复前：不兼容
    b = align_name(["Rakesh Chaturvedi"], [cand("nm2", "Rakesh Chaturvedi Om")], LEVELS2, True)
    assert (b.outcome, b.nconst, b.level) == ("aligned", "nm2", "LS")
    assert align_name(["Rakesh Chaturvedi"], [cand("nm2", "Rakesh Chaturvedi Om")], LEVELS).outcome == "no_match"


def test_ac22_two_subset_hits_are_a_tie_not_an_alignment():
    """同片同角色里两人都与页面名构成 LS：不对齐，页面行交给调用方标 conflict_pending。"""
    a = align_name(["Rakesh Chaturvedi"], [cand("nm1", "Rakesh Chaturvedi Om"), cand("nm2", "Rakesh Chaturvedi Shah")],
                   LEVELS2, True)
    assert a.outcome == "tie_not_aligned" and a.nconst is None and a.level == "LS" and a.tied == ("nm1", "nm2")


def test_ac22_a_stricter_level_beats_a_subset_hit():
    a = align_name(["Rakesh Chaturvedi"], [cand("nm1", "Rakesh Chaturvedi Om"), cand("nm2", "Rakesh Chaturvedi")],
                   LEVELS2, True)
    assert (a.outcome, a.nconst, a.level) == ("aligned", "nm2", "L1")


def test_ac22_single_word_and_middle_word_cases_do_not_become_subset_hits():
    assert align_name(["Madonna"], [cand("nm1", "Madonna Louise")], LEVELS2, True).outcome == "no_match"
    a = align_name(["Michael Jordan"], [cand("nm1", "Michael B. Jordan")], LEVELS2, True)
    assert (a.outcome, a.level) == ("aligned", "L3")                    # 由 level 原有规则处理，不是 LS


def test_ac21_director_signal_t7r2_does_not_light_d_for_format_or_subset_differences():
    page = lambda name: [{"person_id": "p1", "names": [name], "nconst": None}]       # noqa: E731
    imdb = lambda name: [{"nconst": "nm1", "name": name}]                             # noqa: E731
    for page_name, imdb_name, level_ in (("J.P.Dutta", "J.P. Dutta", "L1"),
                                          ("Rakesh Chaturvedi", "Rakesh Chaturvedi Om", "LS")):
        assert director_signal(page(page_name), imdb(imdb_name))[0] == "bad"          # t7r1：亮
        state, detail = director_signal(page(page_name), imdb(imdb_name), True)
        assert state == "ok" and detail["matches"][0]["level"] == level_
    assert director_signal(page("Madonna"), imdb("Madonna Louise"), True)[0] == "bad"  # 单词不触发


def test_ac22_t7r3_directional_ls_in_align_name():
    """t7r3：页面名较短才成立；页面拼接串不得以 LS 对齐，也不再与两个 IMDb 名并列；连字符名不触发。"""
    L3 = ruleset("t7r3").align_levels
    a = align_name(["Rakesh Chaturvedi"], [cand("nm1", "Rakesh Chaturvedi Om")], L3, True, True)
    assert (a.outcome, a.level) == ("aligned", "LS")
    assert align_name(["Whoopi Goldberg Justin Kirk"], [cand("nm1", "Justin Kirk")], L3, True, True).outcome == "no_match"
    assert align_name(["Whoopi Goldberg Justin Kirk"], [cand("nm1", "Justin Kirk")], LEVELS2, True).outcome == "aligned"   # t7r2
    both = [cand("nm1", "George Meeker"), cand("nm2", "Trevor Bardette")]
    assert align_name(["George Meeker Trevor Bardette"], both, L3, True, True).outcome == "no_match"
    assert align_name(["George Meeker Trevor Bardette"], both, LEVELS2, True).outcome == "tie_not_aligned"
    assert align_name(["Carla-Rae"], [cand("nm1", "Carla-Rae Holland")], L3, True, True).outcome == "no_match"
    two = [cand("nm1", "Rakesh Chaturvedi Om"), cand("nm2", "Rakesh Chaturvedi Shah")]
    assert align_name(["Rakesh Chaturvedi"], two, L3, True, True).outcome == "tie_not_aligned"       # 页面较短的并列仍不对齐


def test_ac21_director_signal_t7r3_is_directional():
    page = lambda name: [{"person_id": "p1", "names": [name], "nconst": None}]       # noqa: E731
    imdb = lambda name: [{"nconst": "nm1", "name": name}]                             # noqa: E731
    assert director_signal(page("Rakesh Chaturvedi"), imdb("Rakesh Chaturvedi Om"), True, True)[0] == "ok"
    assert director_signal(page("Rakesh Chaturvedi Om"), imdb("Rakesh Chaturvedi"), True, False)[0] == "ok"    # t7r2
    assert director_signal(page("Rakesh Chaturvedi Om"), imdb("Rakesh Chaturvedi"), True, True)[0] == "bad"   # t7r3
