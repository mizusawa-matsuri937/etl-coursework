"""任务 3 分类规则（spec AC04，及 AC05 的冲突证据部分）：15 个合成页的类别、证据级别与命中规则。"""

from __future__ import annotations

import pytest
from task3.pages import CASES

from etl_task3.extract import extract_fields
from etl_task3.rules import RULES, classify, runtime_minutes


def _classify(body: bytes):
    fields, _ = extract_fields(body)
    return classify(fields)


@pytest.mark.parametrize("name, body, category, basis, rule_id, reason", CASES, ids=[c[0] for c in CASES])
def test_rule_cases(name, body, category, basis, rule_id, reason):
    r = _classify(body)
    assert (r.category, r.basis_level, r.rule_id, r.pending_reason) == (category, basis, rule_id, reason)
    if category != "pending":
        assert any(e.used for e in r.evidence)


def _conflicts(name: str) -> set[tuple[str, str]]:
    body = next(c[1] for c in CASES if c[0] == name)
    r = _classify(body)
    return {(e.signal.name, e.signal.supports) for e in r.evidence
            if not e.used and e.signal.supports and e.signal.supports != r.category}


def test_conflict_evidence_is_kept():
    assert {("box_set", "collection"), ("multi_disc", "collection")} <= _conflicts("box_set_single")
    assert ("breadcrumb_root", "non_movie") in _conflicts("cd_root_vhs_movie")
    assert {("director", "movie"), ("feature_runtime", "movie")} <= _conflicts("tv_genre_with_film_signals")


def test_carrier_in_title_alone_is_not_movie_evidence():
    r = _classify(next(c[1] for c in CASES if c[0] == "title_only_dvd"))
    assert r.category == "pending" and not any(e.signal.supports == "movie" for e in r.evidence)


def test_used_signals_point_to_extracted_fields():
    r = _classify(next(c[1] for c in CASES if c[0] == "structured_movie"))
    used = {e.signal.name: (e.signal.section, e.signal.value_raw) for e in r.evidence if e.used}
    assert used["director"] == ("detail_bullets", "Jane Roe")
    assert used["feature_runtime"] == ("detail_bullets", "1 hour and 50 minutes")
    assert {"director", "feature_runtime", "actors"} <= set(used)


def test_runtime_parsing_and_rule_table():
    assert runtime_minutes("1 hour and 50 minutes") == 110
    assert runtime_minutes("16 hours and 52 minutes") == 1012
    assert runtime_minutes("45 min") == 45
    assert runtime_minutes("NR") is None
    assert all(r.category == "pending" for r in RULES.values() if r.pending_reason)
