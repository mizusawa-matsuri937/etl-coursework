"""任务 2 采集清单：表约束、构建、评论时间、重跑复用与冲突（AC02、AC04–AC06，及 AC01、AC07 的核心部分）。"""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
import pytest

from etl_task1.db import connect, sql_dir
from etl_task1.loader import load_extraction
from etl_task2.manifest import (ManifestConflict, ManifestInputError, apply_task2_schema,
                                build_manifest)

ASINS = ["AAAAAAAAAA", "BBBBBBBBBB", "CCCCCCCCCC", "DDDDDDDDDD", "EEEEEEEEEE", "FFFFFFFFFF"]
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
CRAWLER_COLUMNS = ("final_url", "http_status", "fetched_at", "snapshot_path", "content_hash",
                   "status_reason", "updated_at")


def test_schema_is_applied_once(conn):
    assert apply_task2_schema(conn) is False  # 会话夹具已应用；再次调用不做任何事


@pytest.fixture
def eid(conn, make_settings, verify):
    return load_extraction(make_settings(), verify(), conn).extraction_id


def rows(conn, eid) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    return cur.execute("SELECT * FROM task2.product_manifest WHERE extraction_id = %s "
                       "ORDER BY asin COLLATE \"C\"", (eid,)).fetchall()


def runs(conn, eid) -> list[tuple]:
    return conn.execute(
        "SELECT r.status, r.inserted_rows FROM task2.manifest_run r JOIN task2.manifest_build b USING (build_id) "
        "WHERE b.extraction_id = %s ORDER BY r.started_at, r.run_id", (eid,)).fetchall()


# AC02
def test_fixture_manifest_has_one_row_per_observed_asin(conn, eid):
    result = build_manifest(conn, eid)
    assert (result.status, result.scope, result.inserted_rows, result.total_rows) == ("succeeded", "full", 6, 6)
    got = rows(conn, eid)
    assert [r["asin"] for r in got] == ASINS  # 记录 7 缺产品字段，不产生行；ASIN 原样
    assert {r["asin"]: r["in_valid_records"] for r in got} == {
        "AAAAAAAAAA": True, "BBBBBBBBBB": True, "CCCCCCCCCC": False,
        "DDDDDDDDDD": False, "EEEEEEEEEE": True, "FFFFFFFFFF": True}


def test_source_columns_match_task1_product_summary(conn, eid):
    build_manifest(conn, eid)
    task1 = conn.execute((sql_dir() / "task1_distinct_products.sql").read_text(encoding="utf-8"),
                         {"eid": eid}).fetchall()
    ours = conn.execute(
        "SELECT asin, records_total, records_valid, records_quarantined, first_record_no, last_record_no "
        "FROM task2.product_manifest WHERE extraction_id = %s ORDER BY asin COLLATE \"C\"", (eid,)).fetchall()
    assert ours == [tuple(r[:6]) for r in task1]


# AC04
def test_new_rows_are_not_collected_with_empty_crawl_fields(conn, eid):
    build_manifest(conn, eid)
    for r in rows(conn, eid):
        assert r["status"] == "not_collected" and r["attempt_count"] == 0
        assert all(r[c] is None for c in CRAWLER_COLUMNS), r["asin"]
        assert r["original_url"] == f"https://www.amazon.com/dp/{r['asin']}" and r["url_issue"] is None


@pytest.mark.parametrize("assignment", [
    "status = 'done'",
    "attempt_count = -1",
    "status = 'success', final_url = 'u', http_status = 200, fetched_at = now(), content_hash = 'h'",
])
def test_constraints_reject_illegal_crawler_writes(conn, eid, assignment):
    build_manifest(conn, eid)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(f"UPDATE task2.product_manifest SET {assignment} "
                     "WHERE extraction_id = %s AND asin = 'AAAAAAAAAA'", (eid,))


def test_success_with_complete_evidence_is_accepted(conn, eid):
    build_manifest(conn, eid)
    conn.execute("UPDATE task2.product_manifest SET status = 'success', final_url = 'u', http_status = 200, "
                 "fetched_at = now(), snapshot_path = 'p', content_hash = 'h', attempt_count = 1 "
                 "WHERE extraction_id = %s AND asin = 'AAAAAAAAAA'", (eid,))
    assert conn.execute("SELECT status FROM task2.product_manifest WHERE extraction_id = %s "
                        "AND asin = 'AAAAAAAAAA'", (eid,)).fetchone()[0] == "success"


# AC05
def test_review_times_come_from_valid_records_only(conn, eid):
    build_manifest(conn, eid)
    got = {r["asin"]: (r["earliest_review_time"], r["latest_review_time"]) for r in rows(conn, eid)}
    assert got["AAAAAAAAAA"] == (EPOCH, EPOCH)
    for asin in ("BBBBBBBBBB", "CCCCCCCCCC", "DDDDDDDDDD"):  # 唯一有效记录时间非法 / 只有隔离记录
        assert got[asin] == (None, None), asin
    assert all(r["fetched_at"] is None for r in rows(conn, eid))


# AC06
def test_rebuild_is_reused_and_keeps_crawler_state(conn, eid):
    first = build_manifest(conn, eid)
    conn.execute("UPDATE task2.product_manifest SET status = 'timeout', attempt_count = 3, "
                 "status_reason = 'read timeout', updated_at = now() "
                 "WHERE extraction_id = %s AND asin = 'BBBBBBBBBB'", (eid,))
    before = rows(conn, eid)
    second = build_manifest(conn, eid)
    assert second.build_id == first.build_id
    assert (second.status, second.inserted_rows, second.total_rows) == ("reused", 0, 6)
    assert rows(conn, eid) == before
    b = next(r for r in rows(conn, eid) if r["asin"] == "BBBBBBBBBB")
    assert (b["status"], b["attempt_count"]) == ("timeout", 3)
    assert runs(conn, eid) == [("succeeded", 6), ("reused", 0)]


def test_missing_rows_are_restored_without_touching_others(conn, eid):
    build_manifest(conn, eid)
    conn.execute("UPDATE task2.product_manifest SET status = 'not_found', attempt_count = 1 "
                 "WHERE extraction_id = %s AND asin = 'AAAAAAAAAA'", (eid,))
    conn.execute("DELETE FROM task2.product_manifest WHERE extraction_id = %s AND asin = 'FFFFFFFFFF'", (eid,))
    result = build_manifest(conn, eid)
    assert (result.status, result.inserted_rows, result.total_rows) == ("succeeded", 1, 6)
    a = next(r for r in rows(conn, eid) if r["asin"] == "AAAAAAAAAA")
    assert a["status"] == "not_found"


@pytest.mark.parametrize("tamper", [
    "UPDATE task2.product_manifest SET records_total = records_total + 1, "
    "records_quarantined = records_quarantined + 1 WHERE extraction_id = %(eid)s AND asin = 'AAAAAAAAAA'",
    "UPDATE task2.product_manifest SET original_url = 'https://example.invalid/' "
    "WHERE extraction_id = %(eid)s AND asin = 'AAAAAAAAAA'",
    "INSERT INTO task2.product_manifest (extraction_id, asin, original_url, url_template_version, first_build_id, "
    "in_valid_records, records_total, records_valid, records_quarantined, first_record_no, last_record_no) "
    "SELECT extraction_id, 'ZZZZZZZZZZ', 'https://www.amazon.com/dp/ZZZZZZZZZZ', url_template_version, "
    "first_build_id, true, 1, 1, 0, 1, 1 FROM task2.product_manifest "
    "WHERE extraction_id = %(eid)s AND asin = 'AAAAAAAAAA'",
])
def test_conflicting_existing_rows_fail_without_changes(conn, eid, tamper):
    build_manifest(conn, eid)
    conn.execute(tamper, {"eid": eid})
    before = rows(conn, eid)
    with pytest.raises(ManifestConflict):
        build_manifest(conn, eid)
    assert rows(conn, eid) == before
    assert [s for s, _ in runs(conn, eid)] == ["succeeded", "failed"]
    reason = conn.execute("SELECT failure_reason FROM task2.manifest_run WHERE status = 'failed'").fetchone()[0]
    assert reason.startswith("ManifestConflict")


# AC01（核心检查；Airflow 层的失败记录见 T2-02）
def test_unknown_or_unfinished_extraction_is_rejected(conn, eid):
    with pytest.raises(ManifestInputError, match="不存在"):
        build_manifest(conn, "0" * 64)
    with pytest.raises(ManifestInputError, match="64 位"):
        build_manifest(conn, "not-an-id")
    conn.execute("UPDATE extraction SET status = 'failed' WHERE extraction_id = %s", (eid,))
    with pytest.raises(ManifestInputError, match="未完成"):
        build_manifest(conn, eid)
    assert conn.execute("SELECT count(*) FROM task2.product_manifest").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task2.manifest_build").fetchone()[0] == 0


def test_sample_extraction_requires_explicit_permission(conn, make_settings, verify):
    sample = load_extraction(make_settings(scope="sample", sample_max_records=5), verify(), conn).extraction_id
    with pytest.raises(ManifestInputError, match="样本"):
        build_manifest(conn, sample)
    assert conn.execute("SELECT count(*) FROM task2.product_manifest").fetchone()[0] == 0
    result = build_manifest(conn, sample, allow_sample=True)
    assert result.scope == "sample"
    assert conn.execute("SELECT scope FROM task2.manifest_build WHERE build_id = %s",
                        (result.build_id,)).fetchone()[0] == "sample"


# AC07（事务原子性的核心部分；暂时错误重试与 scheduler 运行见 T2-02）
def test_failure_before_commit_leaves_no_partial_rows(conn, eid):
    def boom():
        raise RuntimeError("injected before commit")

    with pytest.raises(RuntimeError):
        build_manifest(conn, eid, before_commit=boom)
    assert rows(conn, eid) == []
    assert runs(conn, eid) == [("failed", 0)]
    result = build_manifest(conn, eid)
    assert (result.status, result.total_rows) == ("succeeded", 6)


def test_abandoned_running_run_is_closed_by_next_build(conn, eid):
    first = build_manifest(conn, eid)
    conn.execute("INSERT INTO task2.manifest_run (run_id, build_id) VALUES ('stale', %s)", (first.build_id,))
    build_manifest(conn, eid)
    status, reason = conn.execute("SELECT status, failure_reason FROM task2.manifest_run "
                                  "WHERE run_id = 'stale'").fetchone()
    assert status == "failed" and "中断" in reason
