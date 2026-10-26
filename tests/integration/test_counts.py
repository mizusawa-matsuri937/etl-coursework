import json
from pathlib import Path

import pytest

from etl_task1 import validation
from etl_task1.loader import load_extraction

CASES = json.loads((Path(__file__).parents[1] / "fixtures" / "task1_cases.json").read_text())


def loaded(conn, make_settings, verify, **kw):
    return load_extraction(make_settings(**kw), verify(), conn).extraction_id


def test_fixture_counts_follow_spec(conn, make_settings, verify):
    eid = loaded(conn, make_settings, verify)
    c = validation.collect_counts(conn, eid)
    exp = CASES["totals"]
    for key, value in exp.items():
        assert c[key] == value, key
    assert c["records_read"] == c["records_valid"] + c["records_quarantined"] == 10
    assert (c["distinct_products_observed"], c["distinct_users_observed"]) == (6, 8)
    assert (c["distinct_products_valid"], c["distinct_users_valid"]) == (4, 4)
    assert c["product_id_missing"] == 1  # 记录 7 计入读取数，但不生成虚构产品
    assert c["user_id_unknown_literal"] == 1  # 单列频次，不自动转 NULL
    assert (c["products_unidentifiable"], c["users_unidentifiable"]) == (0, 0)


def test_issue_rows_and_issue_records_are_separate_measures(conn, make_settings, verify):
    eid = loaded(conn, make_settings, verify)
    c = validation.collect_counts(conn, eid)
    assert c["quality_issue_rows"] == 9 and c["records_with_quality_issues"] == 7
    dist = {r["code"]: r for r in validation.quality_distribution(conn, eid)}
    assert dist["invalid_utf8"]["issue_rows"] == 1 and dist["empty_user_id"]["records"] == 1
    assert sum(r["issue_rows"] for r in dist.values()) == c["quality_issue_rows"]


def test_invariants_hold_and_baseline_is_not_applied_to_samples(conn, make_settings, verify):
    eid = loaded(conn, make_settings, verify)
    counts, integrity, invariants = validation.collect_and_check(conn, eid)
    assert all(i.ok for i in invariants) and not any(integrity.values())
    sample = validation.compare_baseline(counts, "sample")
    assert sample.status == validation.NOT_APPLICABLE and sample.rows == []


def test_full_scope_baseline_difference_needs_review_and_is_never_forced(conn, make_settings, verify):
    eid = loaded(conn, make_settings, verify)
    counts = validation.collect_counts(conn, eid)
    cmp = validation.compare_baseline(counts, "full")
    assert cmp.status == validation.REVIEW_REQUIRED
    rows = {r["metric"]: r for r in cmp.rows}
    assert rows["records_read"]["measured"] == 10 and rows["records_read"]["difference"] == 10 - 7_911_684
    assert counts["records_read"] == 10  # 实测数未被改动以凑数
    explained = validation.compare_baseline(counts, "full", explanation="夹具只有 10 条")
    assert explained.status == validation.EXPLAINED and explained.explanation
    assert validation.compare_baseline(counts, "full", explanation="   ").status == validation.REVIEW_REQUIRED


def test_baseline_matches_only_when_all_three_equal():
    counts = {"records_read": 7_911_684, "distinct_products_observed": 253_059, "distinct_users_observed": 889_176,
              "distinct_products_valid": 253_000, "distinct_users_valid": 889_000}
    assert validation.compare_baseline(counts, "full").status == validation.MATCHED
    counts["distinct_users_observed"] += 1
    assert validation.compare_baseline(counts, "full").status == validation.REVIEW_REQUIRED


@pytest.mark.parametrize("sql,failed", [
    ("DELETE FROM review_staging WHERE extraction_id = %(e)s AND source_review_id = (SELECT source_review_id FROM review_staging WHERE extraction_id = %(e)s LIMIT 1)",
     {"staging_rows_equal_valid", "integrity_sql_clean"}),
    ("DELETE FROM quarantine_record WHERE extraction_id = %(e)s AND source_review_id = (SELECT source_review_id FROM quarantine_record WHERE extraction_id = %(e)s LIMIT 1)",
     {"quarantine_rows_equal_quarantined", "integrity_sql_clean"}),
])
def test_broken_conservation_is_detected(conn, make_settings, verify, sql, failed):
    eid = loaded(conn, make_settings, verify)
    conn.execute(sql, {"e": eid})
    _, _, invariants = validation.collect_and_check(conn, eid)
    assert {i.name for i in invariants if not i.ok} >= failed


def test_counts_are_scoped_to_one_extraction(conn, make_settings, verify):
    sample = loaded(conn, make_settings, verify, scope="sample", sample_max_records=4)
    full = loaded(conn, make_settings, verify)
    assert validation.collect_counts(conn, sample)["records_read"] == 4
    assert validation.collect_counts(conn, full)["records_read"] == 10
