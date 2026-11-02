"""任务 7 多值字段的纯函数：署名对齐（R38）与覆盖范围（R43）。数据库相关行为见 tests/integration/test_task7_movies_multivalue.py。"""

from __future__ import annotations

import pytest

from etl_task7.movies_multivalue import align_name, cast_coverage
from etl_task7.rules import DEFAULT_RULES

LEVELS = DEFAULT_RULES.align_levels


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
