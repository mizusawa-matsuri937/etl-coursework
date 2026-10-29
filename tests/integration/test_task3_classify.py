"""任务 3 装载（spec AC01、AC02、AC05–AC08）：输入分组、无页面待确认、快照异常、证据、评论保留、
同版本复用与规则版本差异、分批提交与续跑、并发锁。"""

from __future__ import annotations

import dataclasses
import threading

import pytest
from task3.pages import CASES

from _task3_helpers import Product, asin, setup, standard_products, table_counts, task3_counts
from etl_task1.db import connect
from etl_task3 import RULE_VERSION, rules
from etl_task3.classify import (ClassifyBusy, _lock_key, classify_extraction, diff_versions, input_groups, problems,
                                verify)
from etl_task3.rules import classify

pytestmark = pytest.mark.integration


@pytest.fixture
def fx(conn, make_settings, tmp_path):
    return setup(conn, make_settings, tmp_path, standard_products())


def _rows(conn, eid, rv=RULE_VERSION):
    return {r[0]: r[1:] for r in conn.execute(
        "SELECT asin, input_group, category, basis_level, rule_id, pending_reason FROM task3.product_classification "
        "WHERE extraction_id = %s AND rule_version = %s", (eid, rv)).fetchall()}


def test_input_groups_one_per_asin_and_only_given_extraction(conn, make_settings, tmp_path, fx):
    other = setup(conn, make_settings, tmp_path, [Product(asin(90), "dp_page", CASES[0][1]),
                                                  Product(asin(91), "not_collected")], name="b")
    assert input_groups(conn, fx.eid) == {"dp_page": 3, "automation_title": 1, "other_site": 1,
                                          "access_restricted": 1, "not_found": 1, "timeout": 1, "parse_failed": 1,
                                          "not_collected": 1}
    classify_extraction(conn, fx.eid)
    rows = _rows(conn, fx.eid)
    assert len(rows) == 10 and set(rows) == {p.asin for p in fx.products}
    assert conn.execute("SELECT count(*) FROM task3.product_classification WHERE extraction_id = %s",
                        (other.eid,)).fetchone()[0] == 0
    out = verify(conn, fx.eid)
    assert problems(out) == [] and out["category_sum"] == 10


def test_no_page_pending_rows_have_manifest_evidence(conn, fx):
    classify_extraction(conn, fx.eid)
    rows = _rows(conn, fx.eid)
    for p in fx.products[3:]:
        assert rows[p.asin] == (p.group, "pending", "missing", "NP1", p.group)
    assert rows[asin(7)][1] != "non_movie"  # not_found 不判为非电影
    ev = dict(conn.execute(
        "SELECT asin, source_ref FROM task3.classification_evidence WHERE extraction_id = %s AND signal = "
        "'manifest_status' AND used", (fx.eid,)).fetchall())
    assert set(ev) == {p.asin for p in fx.products[3:]}
    assert ev[asin(7)]["status"] == "not_found" and ev[asin(7)]["attempt_no"] == 1
    assert ev[asin(10)]["status"] == "not_collected" and ev[asin(10)]["attempt_no"] is None


def test_snapshot_missing_and_hash_mismatch_are_pending(conn, make_settings, tmp_path):
    body = next(c[1] for c in CASES if c[0] == "structured_movie")
    fx = setup(conn, make_settings, tmp_path, [Product(asin(i), "dp_page", body) for i in (1, 2, 3)])
    fx.snapshots[asin(2)].unlink()
    import gzip
    fx.snapshots[asin(3)].write_bytes(gzip.compress(body[:-1] + b"!"))
    classify_extraction(conn, fx.eid)
    rows = _rows(conn, fx.eid)
    assert rows[asin(1)][1] == "movie"
    assert rows[asin(2)][1:] == ("pending", "missing", "SN1", "snapshot_missing")
    assert rows[asin(3)][1:] == ("pending", "missing", "SN2", "snapshot_hash_mismatch")
    issues = dict(conn.execute("SELECT asin, issue_code FROM task3.quality_issue WHERE extraction_id = %s",
                               (fx.eid,)).fetchall())
    assert issues == {asin(2): "snapshot_missing", asin(3): "snapshot_hash_mismatch"}
    fields = dict(conn.execute("SELECT asin, count(*) FROM task3.page_field WHERE extraction_id = %s GROUP BY 1",
                               (fx.eid,)).fetchall())
    assert set(fields) == {asin(1)}
    assert problems(verify(conn, fx.eid)) == []


def test_evidence_links_to_fields_and_snapshot(conn, make_settings, tmp_path):
    bare = Product(asin(99), "dp_page", b"<html><span id='productTitle'>Fixture</span></html>")
    fx = setup(conn, make_settings, tmp_path,
               [Product(asin(i), "dp_page", c[1]) for i, c in enumerate(CASES, 1)] + [bare])
    classify_extraction(conn, fx.eid)
    assert problems(verify(conn, fx.eid)) == []
    ev = conn.execute("SELECT signal, used, source_ref FROM task3.classification_evidence WHERE extraction_id = %s "
                      "AND asin = %s", (fx.eid, bare.asin)).fetchall()
    assert ev == [("no_signals", True, {"field_count": 1, "field_keys": ["title"]})]
    rows = _rows(conn, fx.eid)
    assert [rows[asin(i)][1:4] for i in range(1, len(CASES) + 1)] == [(c[2], c[3], c[4]) for c in CASES]
    bad = conn.execute(
        """SELECT count(*) FROM task3.classification_evidence e
           LEFT JOIN task3.page_field f USING (extraction_id, asin, parser_version, section, ordinal)
           JOIN task3.page_parse p USING (extraction_id, asin, parser_version)
           WHERE e.extraction_id = %s AND (f.value_raw IS DISTINCT FROM e.value_raw
                 OR p.content_hash IS NULL OR p.final_url IS NULL OR p.fetched_at IS NULL)""",
        (fx.eid,)).fetchone()[0]
    assert bad == 0
    no_used = conn.execute(
        """SELECT count(*) FROM task3.product_classification c WHERE c.extraction_id = %s AND c.category <> 'pending'
           AND NOT EXISTS (SELECT 1 FROM task3.classification_evidence e WHERE e.extraction_id = c.extraction_id
                           AND e.asin = c.asin AND e.rule_version = c.rule_version AND e.used)""",
        (fx.eid,)).fetchone()[0]
    assert no_used == 0
    names = [c[0] for c in CASES]

    def conflicts(name):
        a = asin(names.index(name) + 1)
        return {r[0] for r in conn.execute(
            "SELECT e.signal FROM task3.classification_evidence e JOIN task3.product_classification c "
            "USING (extraction_id, asin, rule_version) WHERE e.extraction_id = %s AND e.asin = %s AND NOT e.used "
            "AND e.supports IS NOT NULL AND e.supports <> c.category", (fx.eid, a)).fetchall()}
    assert {"box_set", "multi_disc"} <= conflicts("box_set_single")
    assert "breadcrumb_root" in conflicts("cd_root_vhs_movie")
    assert {"director", "feature_runtime"} <= conflicts("tv_genre_with_film_signals")


def test_reviews_by_category_and_upstream_untouched(conn, fx):
    before = table_counts(conn)
    classify_extraction(conn, fx.eid)
    assert table_counts(conn) == before
    out = verify(conn, fx.eid)
    assert out["records_read"] == fx.records_read == 12
    assert out["reviews_without_asin"] == 1 and out["reviews_by_category_total"] == 11
    assert problems(out) == []


def test_same_version_rerun_is_reused(conn, fx):
    first = classify_extraction(conn, fx.eid)
    counts = task3_counts(conn, fx.eid)
    second = classify_extraction(conn, fx.eid)
    assert first.status == "succeeded" and second.status == "reused"
    assert second.dp_classified == 0 and second.no_page_classified == 0 and second.parsed == 0
    assert task3_counts(conn, fx.eid) == counts
    assert [r[0] for r in conn.execute("SELECT status FROM task3.classify_run WHERE extraction_id = %s "
                                       "ORDER BY started_at", (fx.eid,)).fetchall()] == ["succeeded", "reused"]


def test_new_rule_version_reclassifies_without_reparsing(conn, fx, monkeypatch):
    classify_extraction(conn, fx.eid)
    before = task3_counts(conn, fx.eid)
    monkeypatch.setattr(rules, "FEATURE_MINUTES", (120, 240))  # 改一个阈值：110 分钟的片子不再算正片片长
    r = classify_extraction(conn, fx.eid, rule_version="t3r_test", classifier=classify)
    after = task3_counts(conn, fx.eid)
    assert r.parsed == 0 and r.reused_parse == 3
    assert after["page_parse"] == before["page_parse"] and after["page_field"] == before["page_field"]
    assert after["product_classification"] == 2 * before["product_classification"]
    changes = diff_versions(conn, fx.eid, RULE_VERSION, "t3r_test")
    assert [c["asin"] for c in changes] == [asin(1)]
    assert changes[0]["old_category"] == "movie" and changes[0]["new_category"] == "pending"
    assert _rows(conn, fx.eid)[asin(1)][1] == "movie"  # 旧版本仍可查询


def test_batch_failure_leaves_no_partial_batch_and_resume_finishes(conn, fx):
    def boom(batch_no):
        if batch_no == 2:
            raise RuntimeError("injected before commit of batch 2")
    with pytest.raises(RuntimeError, match="batch 2"):
        classify_extraction(conn, fx.eid, batch_size=2, before_commit=boom)
    dp = dict(conn.execute("SELECT asin, rule_id FROM task3.product_classification WHERE extraction_id = %s "
                           "AND input_group = 'dp_page'", (fx.eid,)).fetchall())
    assert set(dp) == {asin(1), asin(2)}  # 只有第 1 批
    assert conn.execute("SELECT count(*) FROM task3.page_parse WHERE extraction_id = %s",
                        (fx.eid,)).fetchone()[0] == 2
    assert conn.execute("SELECT status FROM task3.classify_run WHERE extraction_id = %s",
                        (fx.eid,)).fetchone()[0] == "failed"
    resumed = classify_extraction(conn, fx.eid, batch_size=2)
    assert resumed.dp_classified == 1 and resumed.no_page_classified == 0
    assert task3_counts(conn, fx.eid)["product_classification"] == 10
    assert problems(verify(conn, fx.eid)) == []


def test_limit_leaves_remaining_and_verify_fails(conn, fx):
    r = classify_extraction(conn, fx.eid, batch_size=1, limit=2)
    assert r.dp_classified == 2 and r.remaining_dp == 1
    assert any("缺 1 个" in p for p in problems(verify(conn, fx.eid)))


def test_abandoned_running_run_is_marked_interrupted(conn, fx):
    conn.execute("INSERT INTO task3.classify_run (run_id, extraction_id, parser_version, label_map_version, "
                 "rule_version) VALUES ('stale', %s, 'p', 'm', %s)", (fx.eid, RULE_VERSION))
    classify_extraction(conn, fx.eid)
    assert conn.execute("SELECT status FROM task3.classify_run WHERE run_id = 'stale'").fetchone()[0] == "interrupted"


def test_concurrent_run_cannot_take_lock(conn, base_settings, fx):
    held, release = threading.Event(), threading.Event()

    def hold():
        with connect(base_settings) as other:
            other.execute("SELECT pg_advisory_lock(%s)", (_lock_key(fx.eid, RULE_VERSION),))
            held.set()
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert held.wait(10)
        with pytest.raises(ClassifyBusy):
            classify_extraction(conn, fx.eid)
    finally:
        release.set()
        t.join()
    assert conn.execute("SELECT count(*) FROM task3.product_classification WHERE extraction_id = %s",
                        (fx.eid,)).fetchone()[0] == 0


def test_summary_dataclass_is_serializable(conn, fx):
    assert dataclasses.asdict(classify_extraction(conn, fx.eid))["status"] == "succeeded"
